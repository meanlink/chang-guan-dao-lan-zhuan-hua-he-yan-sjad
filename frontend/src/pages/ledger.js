import {ledger} from '../api/ledger.js';
import {api, fetchFile, saveBlob} from '../api/client.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc, date, notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const STATUS_CLASS = {complete: 'ok', damaged: 'bad', missing_anchor: 'warn'};
const STATUS_TEXT = {complete: '完整', damaged: '受损', missing_anchor: '缺少可信依据'};

function statusChip(verification) {
  if (!verification) return '<span class="ledger-chip keep">待复核</span>';
  return '<span class="ledger-chip ' + (STATUS_CLASS[verification.status] || 'keep') + '">' + esc(verification.label || STATUS_TEXT[verification.status] || verification.status) + '</span>';
}

function problems(verification) {
  if (!verification?.problems?.length) return '';
  return '<ul class="ledger-problems">' + verification.problems.map(p => '<li>' + (p.seq ? '<b>第 ' + p.seq + ' 条</b> ' : '') + esc(p.message) + '</li>').join('') + '</ul>';
}

function packageCard(item) {
  const admin = session.user.role === 'admin';
  const v = item.verification;
  return '<article class="ledger-package" data-package="' + item.id + '">' +
    '<div class="ledger-package-head"><div><strong>截至第 ' + item.cutoff_seq + ' 条</strong><p>' + item.entry_count + ' 条凭据 · ' + esc(item.created_by) + ' · ' + date(item.created_at) + '</p>' + (item.anchored ? '<small>可信锚点已另存 · 公钥指纹 ' + esc(item.key_id) + '</small>' : '<small>尚未另存可信锚点</small>') + '</div>' + statusChip(v) + '</div>' +
    '<p class="ledger-summary">' + esc(v?.summary || '') + '</p>' + problems(v) +
    '<div class="ledger-actions">' +
      (item.download_ready ? '<button class="text-button" data-pkg-download="' + item.id + '">下载核验包 ↓</button>' : '<button class="text-button" disabled>核验包未就绪</button>') +
      (admin && item.status === 'complete' ? '<button class="text-button" data-pkg-anchor="' + item.id + '">' + (item.anchored ? '重新另存锚点' : '另存可信锚点') + '</button>' : '') +
      (item.anchored ? '<button class="text-button" data-anchor-download="' + item.id + '">下载锚点 ↓</button>' : '') +
      '<button class="text-button" data-pkg-verify="' + item.id + '">重新复核</button>' +
    '</div></article>';
}

export async function ledgerPage() {
  const [overview, packages, entries, audits] = await Promise.all([
    ledger.overview(), ledger.packages(), ledger.entries(0, 40), api('/audits'),
  ]);
  workspace.packages = packages;
  const admin = session.user.role === 'admin';
  const head = overview.head_hash ? overview.head_hash.slice(0, 16) + '…' : '尚无凭据';
  const key = overview.key;
  return heading('06 / EVIDENCE LEDGER', '防篡改凭据与核验包',
    '客流接入、核验完成、批准结论与简报归档各落一条链式凭据；业务与凭据同一事务提交。负责人可截取截至指定序号的核验包并另存可信锚点，离线就能查出改写、缺项、调序与尾部截断。') +
    '<section class="ledger-stats">' +
      '<div><span>当前序号</span><strong>' + overview.last_seq + '</strong></div>' +
      '<div><span>链末端哈希</span><strong class="mono">' + esc(head) + '</strong></div>' +
      '<div><span>验签公钥指纹</span><strong class="mono">' + esc(key ? key.key_id : '尚未生成') + '</strong></div>' +
      '<div><span>受保护动作</span><strong>' + esc(Object.entries(overview.events).map(([k, v]) => k + ' ' + v).join(' · ') || '—') + '</strong></div>' +
    '</section>' +
    '<div class="ledger-grid"><section class="panel"><div class="section-title"><h2>截取核验包</h2><span>仅业务负责人</span></div>' +
      '<form id="package-form"><label>截至序号<input type="number" name="cutoff_seq" min="1" max="' + Math.max(overview.last_seq, 1) + '" value="' + overview.last_seq + '" required ' + (admin ? '' : 'disabled') + '></label>' +
      '<button type="submit" class="primary" ' + (admin && overview.last_seq ? '' : 'disabled') + '>制作核验包</button>' +
      '<p class="hint">同一截止序号只对应一个核验包：重试复用原包，不重复办理、不增加内容；出包中断后按同次内容续写，之后到达的凭据留待下一包。</p></form>' +
      '<div class="section-title"><h2>最近凭据</h2><span>按唯一序号连续衔接</span></div>' +
      '<ol class="ledger-list">' + (entries.items.length ? entries.items.slice().reverse().map(e => '<li><span class="seq">#' + e.seq + '</span><div><strong>' + esc(e.event_label || e.event) + '</strong><small>' + esc(e.actor) + ' · ' + esc(e.subject) + ' · ' + date(e.created_at) + '</small><code>' + esc(e.entry_hash.slice(0, 20)) + '…</code></div></li>').join('') : '<li class="empty">尚无凭据。</li>') + '</ol>' +
    '</section><section>' +
      '<div class="section-title"><h2>已制作核验包</h2><span>' + packages.length + ' 份</span></div>' +
      '<div class="ledger-packages">' + (packages.length ? packages.map(packageCard).join('') : '<p class="empty">还没有核验包。选择截至序号后制作第一份。</p>') + '</div>' +
      '<section class="panel"><div class="section-title"><h2>本地离线检查</h2><span>只用上传的核验包与锚点</span></div>' +
        '<form id="check-form"><label>核验包 JSON<input type="file" name="package" accept=".json,application/json" required></label>' +
        '<label>可信锚点 JSON（可选）<input type="file" name="anchor" accept=".json,application/json"></label>' +
        '<label>预期公钥指纹（可选，防止锚点被替换）<input name="expect_key_id" maxlength="32" placeholder="例如 ' + esc(key ? key.key_id : '3f1ff94e0ee17bd7') + '"></label>' +
        '<button type="submit" class="primary">检查核验包</button>' +
        '<p class="hint">检查只读取这两份文件本身，不查询业务库。命令行同样可用：<code>python -m scripts.ledger_verify 核验包.json --anchor 锚点.json</code>。</p></form>' +
        '<div id="check-result"></div></section>' +
      '<section class="panel history"><div class="section-title"><h2>历史操作日志</h2><span>仅供阅读 · 非受保护凭据</span></div>' +
        '<ul class="timeline">' + audits.slice(0, 8).map(a => '<li><strong>' + esc(a.action) + '</strong><span>' + esc(a.actor) + ' · ' + date(a.at) + '</span></li>').join('') + '</ul>' +
        '<p class="hint">这些是旧操作日志，没有链式摘要，可能被补写或删改，不能作为防篡改凭据，也不进入核验包。上方“最近凭据”才是与业务同事务落定的受保护记录。</p></section>' +
    '</section></div>';
}

async function readJson(file) {
  const text = await file.text();
  try { return JSON.parse(text); } catch { throw new Error('文件 ' + file.name + ' 不是有效 JSON'); }
}

export function bindLedger(refresh) {
  document.querySelectorAll('[data-pkg-download]').forEach(button => button.onclick = async () => {
    try {
      const item = workspace.packages?.find(p => String(p.id) === button.dataset.pkgDownload);
      const blob = await fetchFile('/ledger/packages/' + button.dataset.pkgDownload + '/download');
      saveBlob(blob, item?.filename || '核验包-' + button.dataset.pkgDownload + '.json');
    } catch (error) { notify(error.message, true); }
  });
  document.querySelectorAll('[data-anchor-download]').forEach(button => button.onclick = async () => {
    try {
      const blob = await fetchFile('/ledger/packages/' + button.dataset.anchorDownload + '/anchor/download');
      saveBlob(blob, '可信锚点-' + button.dataset.anchorDownload + '.json');
    } catch (error) { notify(error.message, true); }
  });
  document.querySelectorAll('[data-pkg-anchor]').forEach(button => button.onclick = async () => {
    button.disabled = true;
    try {
      const result = await ledger.anchor(button.dataset.pkgAnchor);
      notify(result.reused ? '该截止序号已有可信锚点，已复用' : '可信锚点已另存，请单独保管');
      await refresh();
    } catch (error) { notify(error.message, true); button.disabled = false; }
  });
  document.querySelectorAll('[data-pkg-verify]').forEach(button => button.onclick = async () => {
    try {
      const result = await ledger.verifyPackage(button.dataset.pkgVerify);
      notify(result.summary, result.status !== 'complete');
      await refresh();
    } catch (error) { notify(error.message, true); }
  });
  const form = document.querySelector('#package-form');
  if (form) submit(form, async f => {
    const result = await ledger.createPackage(Number(f.get('cutoff_seq')));
    notify(result.reused ? '该截止序号已有核验包，已复用原包' : '核验包已制作，可下载并另存可信锚点');
    await refresh();
  });
  const check = document.querySelector('#check-form');
  if (check) submit(check, async f => {
    const packageFile = f.get('package');
    if (!packageFile || !packageFile.size) throw new Error('请选择核验包文件');
    const anchorFile = f.get('anchor');
    const body = {package: await readJson(packageFile), anchor: null};
    if (anchorFile && anchorFile.size) body.anchor = await readJson(anchorFile);
    const expect = String(f.get('expect_key_id') || '').trim();
    if (expect) body.expect_key_id = expect;
    const result = await ledger.verifyUpload(body);
    document.querySelector('#check-result').innerHTML = '<div class="ledger-verdict ' + (STATUS_CLASS[result.status] || 'keep') + '"><strong>' + esc(result.label) + '</strong><p>' + esc(result.summary) + '</p></div>' + problems(result);
  });
}
