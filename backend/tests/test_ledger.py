"""防篡改凭据链、核验包与离线核验的工程测试。

在 backend/ 目录运行：

    DATABASE_URL=sqlite:////tmp/ledger-test.sqlite ARTIFACT_DIR=/tmp/ledger-artifacts \
    JWT_SECRET=test-secret python3 -m unittest discover -s tests -v

覆盖：连续编号与并发衔接、业务与凭据同事务回滚、幂等重试、
核验包截取/复用/中断续写、离线定位改写·缺项·调序·尾部截断、
可信锚点签名与公钥替换、角色权限。
"""

import copy
import json
import os
import tempfile
import threading
import unittest

os.environ.setdefault('JWT_SECRET', 'test-secret')
TEMP_ROOT = tempfile.mkdtemp(prefix='ledger-tests-')
os.environ['DATABASE_URL'] = f'sqlite:///{TEMP_ROOT}/db.sqlite'
os.environ['ARTIFACT_DIR'] = os.path.join(TEMP_ROOT, 'artifacts')

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src.config.database import Session  # noqa: E402
from src.main import app  # noqa: E402
from src.models import Job, LedgerEntry, LedgerPackage, LedgerState  # noqa: E402
from src.services import ledger, ledger_package as pkg  # noqa: E402

RECORDS = [
    {'record': f'r{i}', 'zone': '星空厅', 'arm': 'A' if i % 2 else 'B', 'visitors': 60 + i, 'completed': 30 + (i % 20)}
    for i in range(10)
]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.admin = cls.headers('admin@demo.local')
        cls.analyst = cls.headers('analyst@demo.local')
        cls.viewer = cls.headers('viewer@demo.local')

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    @classmethod
    def headers(cls, email):
        response = cls.client.post('/auth/login', json={'email': email, 'password': 'Demo123!'})
        assert response.status_code == 200, response.text
        return {'Authorization': 'Bearer ' + response.json()['access_token']}

    def ingest(self, name, key=None, headers=None):
        return self.client.post(
            '/datasets',
            json={'name': name, 'source': 'test.json', 'records': RECORDS},
            headers={**(headers or self.admin), **({'Idempotency-Key': key} if key else {})},
        )

    def seqs(self):
        with Session() as db:
            return [row.seq for row in db.scalars(select(LedgerEntry).order_by(LedgerEntry.seq))]

    def last_seq(self):
        with Session() as db:
            return db.get(LedgerState, 1).last_seq


class ChainTests(Base):
    def test_seed_chain_covers_business_events(self):
        entries = self.client.get('/ledger/entries?limit=50', headers=self.admin).json()['items']
        events = [entry['event'] for entry in entries]
        for expected in ('dataset.ingest', 'job.verified', 'job.approved', 'report.archived'):
            self.assertIn(expected, events)

    def test_concurrent_ingest_keeps_unique_sequence(self):
        before = self.last_seq()
        codes = []

        def worker(index):
            codes.append(self.ingest(f'并发批次-{index}', key=f'concurrent-{index}').status_code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertEqual(codes.count(201), 6, codes)
        sequences = self.seqs()
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        self.assertEqual(len(sequences), before + 6)

    def test_failed_business_write_leaves_no_credential(self):
        before = self.last_seq()
        duplicate = self.client.post(
            '/datasets',
            json={'name': '三展区 · 20 日 A/B 导览计数', 'source': 'test.json', 'records': RECORDS},
            headers={**self.admin, 'Idempotency-Key': 'duplicate-name'},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self.last_seq(), before)

    def test_idempotent_retry_does_not_duplicate(self):
        before = self.last_seq()
        first = self.ingest('幂等批次', key='idem-1')
        second = self.ingest('幂等批次-另一个名字', key='idem-1')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertTrue(second.json()['replayed'])
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(self.last_seq(), before + 1)
        names = {d['name'] for d in self.client.get('/datasets', headers=self.admin).json()}
        self.assertNotIn('幂等批次-另一个名字', names)

    def test_same_key_sent_concurrently_runs_once(self):
        before = self.last_seq()
        codes, ids = [], []
        lock = threading.Lock()

        def worker(index):
            response = self.ingest('同键并发批次', key='same-key-race')
            with lock:
                codes.append(response.status_code)
                if response.status_code == 201:
                    ids.append(response.json()['id'])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertEqual(set(codes) - {201, 409}, set(), codes)
        self.assertEqual(len(set(ids)), 1, ids)
        self.assertEqual(self.last_seq(), before + 1)

    def test_worker_verification_is_chained_with_result(self):
        job = self.client.post(
            '/jobs',
            json={'name': '链上核验', 'dataset_id': 1, 'params': {'zone': '全部', 'alpha': '0.05'}},
            headers={**self.analyst, 'Idempotency-Key': 'job-chain-1'},
        ).json()
        from src.worker import execute

        execute(job['id'])
        with Session() as db:
            stored = db.get(Job, job['id'])
            self.assertEqual(stored.status, 'completed')
            row = db.scalar(select(LedgerEntry).where(LedgerEntry.dedupe_key == f"job.verified:{job['id']}"))
            self.assertIsNotNone(row)
            self.assertEqual(row.payload['result_checksum'], pkg.sha256_hex(pkg.canonical_json(stored.result)))
        entry = self.client.get('/jobs/' + str(job['id']), headers=self.analyst).json()
        self.assertEqual(entry['status'], 'completed')


class PackageTests(Base):
    counter = 0

    def advance(self):
        PackageTests.counter += 1
        self.assertEqual(self.ingest(f'推进批次-{PackageTests.counter}', key=f'advance-{PackageTests.counter}').status_code, 201)
        return self.last_seq()

    def make_package(self, cutoff=None):
        cutoff = cutoff or self.advance()
        response = self.client.post('/ledger/packages', json={'cutoff_seq': cutoff}, headers=self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_retry_reuses_same_package(self):
        cutoff = self.advance()
        first = self.make_package(cutoff)
        second = self.make_package(cutoff)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second['reused'])
        with Session() as db:
            count = len(list(db.scalars(select(LedgerPackage).where(LedgerPackage.cutoff_seq == cutoff))))
        self.assertEqual(count, 1)
        self.assertEqual(first['entry_count'], cutoff)

    def test_anchor_and_offline_verification(self):
        package = self.make_package()
        self.assertEqual(package['verification']['status'], 'missing_anchor')
        anchored = self.client.post(f"/ledger/packages/{package['id']}/anchor", headers=self.admin).json()
        self.assertEqual(anchored['verification']['status'], 'complete')
        again = self.client.post(f"/ledger/packages/{package['id']}/anchor", headers=self.admin).json()
        self.assertTrue(again['reused'])
        self.assertEqual(again['anchor']['signature'], anchored['anchor']['signature'])

        document = json.loads(self.client.get(f"/ledger/packages/{package['id']}/download", headers=self.admin).content)
        anchor = json.loads(self.client.get(f"/ledger/packages/{package['id']}/anchor/download", headers=self.admin).content)
        report = self.client.post('/ledger/verify', json={'package': document, 'anchor': anchor}, headers=self.admin).json()
        self.assertEqual(report['status'], 'complete')

    def test_interrupted_package_resumes_same_content(self):
        cutoff = self.advance()
        with Session() as db:
            package = LedgerPackage(cutoff_seq=cutoff, status='building', cursor=1, filename='', created_by=1, created_at=1700000000.5)
            db.add(package)
            db.commit()
            package_id = package.id
        path = os.path.join(os.environ['ARTIFACT_DIR'], ledger.package_filename(package_id, cutoff))
        os.makedirs(os.environ['ARTIFACT_DIR'], exist_ok=True)
        with Session() as db:
            first_two = [ledger.entry_document(row) for row in ledger.entries_through(db, cutoff)][:2]
        with open(path + '.partial', 'w', encoding='utf-8') as stream:
            stream.write(''.join(pkg.canonical_json(entry) + '\n' for entry in first_two))
            stream.write('{"seq": 3, "event": "torn')  # 模拟进程在写单行途中被杀

        response = self.client.post('/ledger/packages', json={'cutoff_seq': cutoff}, headers=self.admin)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertTrue(response.json()['reused'])
        self.assertEqual(response.json()['status'], 'complete')
        document = json.loads(self.client.get(f'/ledger/packages/{package_id}/download', headers=self.admin).content)
        self.assertEqual([entry['seq'] for entry in document['entries']], list(range(1, cutoff + 1)))
        self.assertEqual(document['created_at'], 1700000000.5)
        self.assertFalse(os.path.exists(path + '.partial'))

        later_cutoff = self.advance()
        later = self.make_package(later_cutoff)
        self.assertFalse(later['reused'])
        later_document = json.loads(self.client.get(f"/ledger/packages/{later['id']}/download", headers=self.admin).content)
        # 后到的凭据只进入下一包，旧包保持截取时内容
        self.assertEqual(later_document['entry_count'], later_cutoff)
        self.assertEqual(document['entry_count'], cutoff)

    def test_only_admin_may_cut_packages(self):
        cutoff = self.advance()
        self.assertEqual(self.client.post('/ledger/packages', json={'cutoff_seq': cutoff}, headers=self.analyst).status_code, 403)
        self.assertEqual(self.client.post('/ledger/packages', json={'cutoff_seq': cutoff}, headers=self.viewer).status_code, 403)
        self.assertEqual(self.client.post('/ledger/packages', json={'cutoff_seq': cutoff + 999}, headers=self.admin).status_code, 422)


class OfflineVerificationTests(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cutoff = cls.client.get('/ledger', headers=cls.admin).json()['last_seq']
        package = cls.client.post('/ledger/packages', json={'cutoff_seq': cutoff}, headers=cls.admin).json()
        cls.anchor = cls.client.post(f"/ledger/packages/{package['id']}/anchor", headers=cls.admin).json()['anchor']
        cls.document = json.loads(cls.client.get(f"/ledger/packages/{package['id']}/download", headers=cls.admin).content)
        cls.key_id = cls.anchor['key_id']

    def verify(self, document, anchor='default', expect=None):
        payload = {'package': document, 'anchor': self.anchor if anchor == 'default' else anchor}
        if expect:
            payload['expect_key_id'] = expect
        return self.client.post('/ledger/verify', json=payload, headers=self.admin).json()

    def codes(self, report):
        return {problem['code'] for problem in report['problems']}

    def test_unmodified_package_is_complete(self):
        report = self.verify(self.document)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['problems'], [])

    def test_without_anchor_is_missing_evidence(self):
        report = self.client.post('/ledger/verify', json={'package': self.document}, headers=self.admin).json()
        self.assertEqual(report['status'], 'missing_anchor')

    def test_modified_text_is_located(self):
        document = copy.deepcopy(self.document)
        document['entries'][1]['payload']['params']['alpha'] = '0.1'
        report = self.verify(document)
        self.assertEqual(report['status'], 'damaged')
        self.assertTrue({'payload_modified', 'package_hash_mismatch'} & self.codes(report))
        self.assertTrue(any(problem.get('seq') == 2 for problem in report['problems']))

    def test_removed_entry_is_located(self):
        document = copy.deepcopy(self.document)
        document['entries'].pop(1)
        report = self.verify(document)
        self.assertEqual(report['status'], 'damaged')
        self.assertIn('entry_count_mismatch', self.codes(report))
        self.assertIn('missing_entry', self.codes(report))

    def test_reordered_entries_are_located(self):
        document = copy.deepcopy(self.document)
        document['entries'][1], document['entries'][2] = document['entries'][2], document['entries'][1]
        report = self.verify(document)
        self.assertEqual(report['status'], 'damaged')
        self.assertIn('out_of_order', self.codes(report))

    def test_tail_truncation_is_located(self):
        document = copy.deepcopy(self.document)
        document['entries'].pop()
        report = self.verify(document)
        self.assertEqual(report['status'], 'damaged')
        self.assertIn('tail_truncated', self.codes(report))

    def test_rebuilt_package_needs_matching_anchor(self):
        document = copy.deepcopy(self.document)
        document['entries'] = document['entries'][:-1]
        header = {key: document.get(key) for key in pkg.PACKAGE_HEADER_FIELDS}
        header['entry_count'] = len(document['entries'])
        header['head_hash'] = document['entries'][-1]['entry_hash']
        document.update(header)
        document['cutoff_seq'] = header['cutoff_seq'] - 1
        document['head_hash'] = header['head_hash']
        document['package_hash'] = pkg.package_digest({**header, 'cutoff_seq': document['cutoff_seq']}, document['entries'])
        report = self.verify(document, anchor=None)
        self.assertEqual(report['status'], 'missing_anchor')
        report = self.verify(document)
        self.assertEqual(report['status'], 'damaged')
        self.assertIn('anchor_mismatch', self.codes(report))

    def test_unexpected_public_key_is_rejected(self):
        report = self.verify(self.document, expect='0000000000000000')
        self.assertEqual(report['status'], 'damaged')
        self.assertIn('anchor_key_unexpected', self.codes(report))

    def test_tampered_anchor_signature_is_rejected(self):
        anchor = copy.deepcopy(self.anchor)
        anchor['package_hash'] = 'f' * 64
        report = self.verify(self.document, anchor=anchor)
        self.assertEqual(report['status'], 'damaged')
        self.assertIn('anchor_signature_invalid', self.codes(report))

    def test_offline_cli_runs_without_database(self):
        from scripts import ledger_verify

        package_path = os.path.join(TEMP_ROOT, 'cli-package.json')
        anchor_path = os.path.join(TEMP_ROOT, 'cli-anchor.json')
        with open(package_path, 'w', encoding='utf-8') as stream:
            json.dump(self.document, stream)
        with open(anchor_path, 'w', encoding='utf-8') as stream:
            json.dump(self.anchor, stream)
        code = ledger_verify.main([package_path, '--anchor', anchor_path, '--key-id', self.key_id, '--json'])
        self.assertEqual(code, 0)
        broken = copy.deepcopy(self.document)
        broken['entries'].pop()
        with open(package_path, 'w', encoding='utf-8') as stream:
            json.dump(broken, stream)
        code = ledger_verify.main([package_path, '--anchor', anchor_path, '--json'])
        self.assertEqual(code, 1)


class BusinessIntegrationTests(Base):
    def test_approval_and_archive_are_chained(self):
        job = self.client.post(
            '/jobs',
            json={'name': '批准链测试', 'dataset_id': 1, 'params': {'zone': '水世界', 'alpha': '0.05'}},
            headers={**self.admin, 'Idempotency-Key': 'approve-1'},
        ).json()
        from src.worker import execute

        execute(job['id'])
        before = self.last_seq()
        approved = self.client.post(f"/jobs/{job['id']}/approve", headers={**self.admin, 'Idempotency-Key': 'ap-1'}).json()
        self.assertEqual(self.last_seq(), before + 1)
        self.assertEqual(approved['ledger_seq'], before + 1)
        again = self.client.post(f"/jobs/{job['id']}/approve", headers={**self.admin, 'Idempotency-Key': 'ap-2'}).json()
        self.assertEqual(self.last_seq(), before + 1, '重复批准不得新增凭据')
        self.assertEqual(again['ledger_seq'], approved['ledger_seq'], '重复批准只回指首次凭据')

        scheduled = self.client.post(
            '/reports',
            json={'name': '归档链测试', 'job_id': job['id'], 'destination': '测试组', 'interval_seconds': 60, 'delay_seconds': 0},
            headers=self.admin,
        )
        self.assertEqual(scheduled.status_code, 201)
        from src.worker import tick

        tick()
        with Session() as db:
            row = db.scalar(select(LedgerEntry).where(LedgerEntry.event == 'report.archived', LedgerEntry.payload['job_id'].as_integer() == job['id']))
            self.assertIsNotNone(row)
        sequences = self.seqs()
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))


if __name__ == '__main__':
    unittest.main()
