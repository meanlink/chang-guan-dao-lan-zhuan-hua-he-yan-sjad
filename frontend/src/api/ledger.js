import {api} from './client.js';

const json = body => ({method: 'POST', body: JSON.stringify(body)});

export const ledger = {
  overview: () => api('/ledger'),
  entries: (after = 0, limit = 50) => api('/ledger/entries?after=' + after + '&limit=' + limit),
  key: () => api('/ledger/anchor-key'),
  packages: () => api('/ledger/packages'),
  createPackage: cutoff_seq => api('/ledger/packages', json({cutoff_seq})),
  package: id => api('/ledger/packages/' + id),
  verifyPackage: id => api('/ledger/packages/' + id + '/verify'),
  anchor: id => api('/ledger/packages/' + id + '/anchor', {method: 'POST'}),
  verifyUpload: body => api('/ledger/verify', json(body)),
};
