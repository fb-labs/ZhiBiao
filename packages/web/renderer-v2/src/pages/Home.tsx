import { useEffect, useMemo, useState } from 'react';
import { Backdrop } from '../components/Backdrop';
import { api, type Account, type Overview, type Tender } from '../api';

const STATUS_LABEL: Record<string, string> = {
  intake: '资料接收',
  analyzed: '解读完成',
  outlined: '大纲完成',
  drafting: '编写中',
  review: '检查中',
  delivery: '交付中',
  complete: '已完成',
  archived: '已归档',
};

function statusLabel(status: string) {
  return STATUS_LABEL[status] || status;
}

type Props = {
  account: Account;
  onLogout: () => void;
};

export function HomePage({ account, onLogout }: Props) {
  const [tenders, setTenders] = useState<Tender[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);

  const [title, setTitle] = useState('');
  const [buyer, setBuyer] = useState('');
  const [budget, setBudget] = useState('');
  const [deadline, setDeadline] = useState('');
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [list, stats] = await Promise.all([api.tenders(), api.overview()]);
      setTenders(list.items);
      setOverview(stats);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return tenders;
    return tenders.filter((item) =>
      [item.title, item.buyer, item.code].some((value) => (value || '').toLowerCase().includes(keyword)),
    );
  }, [tenders, query]);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!title.trim() || busy) return;
    setBusy(true);
    setError('');
    try {
      const tender = await api.createTender({
        title: title.trim(),
        buyer: buyer.trim(),
        budget: budget.trim(),
        deadline: deadline.trim(),
      });
      setCreating(false);
      setTitle('');
      setBuyer('');
      setBudget('');
      setDeadline('');
      window.location.href = `/tender/${tender.id}`;
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  const active = tenders.filter((item) => !['complete', 'archived'].includes(item.status)).length;
  const completed = tenders.filter((item) => item.status === 'complete').length;

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brand-mark">智</span>
          <span className="brand-copy">
            <strong>智标</strong>
            <small>BID INTELLIGENCE</small>
          </span>
        </a>
        <nav className="topnav">
          <a className="active" href="/">工作台</a>
          {(account.isSuperuser || account.permissions.includes('admin.ai')) && (
            <a href="/settings/ai">模型服务</a>
          )}
          {account.permissions.includes('admin.accounts') && (
            <a href="/settings/accounts">账号权限</a>
          )}
        </nav>
        <div className="topbar-right">
          <label className="search">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索项目名称、编号或采购人"
            />
          </label>
          <span className="account-chip" title={account.displayName}>
            {account.displayName.slice(0, 1)}
          </span>
          <button className="ghost-button" onClick={onLogout}>退出</button>
        </div>
      </header>

      <main className="workspace">
        <section className="hero">
          <Backdrop />
          <div className="hero-copy">
            <p className="eyebrow">投标工作台</p>
            <h1>
              {account.displayName}，今天从
              <br />
              哪一份标书开始？
            </h1>
            <p className="lead">
              新增标书后，上传招标文件即可开始解读。所有项目按新增时间排列，最新的在最前。
            </p>
          </div>
          <div className="hero-stats">
            <div><span>项目总数</span><strong>{overview?.total ?? 0}</strong></div>
            <div><span>进行中</span><strong>{active}</strong></div>
            <div><span>已完成</span><strong>{completed}</strong></div>
            <div><span>资料文件</span><strong>{overview?.files ?? 0}</strong></div>
          </div>
        </section>

        <section className="library">
          <div className="section-head">
            <div>
              <h2>投标书</h2>
              <p>第一张为新增入口，其余按新增时间由近到远排列</p>
            </div>
            <span className="count">{filtered.length} 份</span>
          </div>

          {error && <p className="notice error">{error}</p>}
          {loading ? (
            <p className="notice">正在加载投标书…</p>
          ) : (
            <div className="sheet-rail">
              <button className="sheet sheet-new" onClick={() => setCreating(true)}>
                <span className="sheet-plus">+</span>
                <strong>新增标书</strong>
                <small>创建一份新的投标项目</small>
              </button>
              {filtered.map((tender, index) => (
                <a className="sheet" key={tender.id} href={`/tender/${tender.id}`}>
                  <div className="sheet-top">
                    <span className="sheet-kicker">BID DOCUMENT</span>
                    {index === 0 && query === '' && <span className="sheet-flag">最近增加</span>}
                  </div>
                  <h3>{tender.title}</h3>
                  <span className={`status status-${tender.status}`}>{statusLabel(tender.status)}</span>
                  <div className="sheet-meta">
                    <span>{tender.code}</span>
                    <span>{tender.createdAt ? tender.createdAt.slice(0, 10) : '—'}</span>
                  </div>
                  <span className="sheet-open">进入 →</span>
                </a>
              ))}
              {filtered.length === 0 && query !== '' && (
                <p className="notice">没有匹配的投标书</p>
              )}
            </div>
          )}
        </section>

        <section className="insights">
          <div className="panel">
            <div className="panel-head">
              <h3>项目增长</h3>
              <span>近 6 个月</span>
            </div>
            <div className="mini-chart">
              {(overview?.monthly || []).map((item) => {
                const max = Math.max(1, ...(overview?.monthly || []).map((point) => point.count));
                return (
                  <div className="mini-bar" key={item.month}>
                    <div style={{ height: `${Math.max(6, (item.count / max) * 100)}%` }} />
                    <small>{item.month.slice(5)}</small>
                    <em>{item.count}</em>
                  </div>
                );
              })}
            </div>
          </div>
          <div className="panel">
            <div className="panel-head">
              <h3>项目状态</h3>
              <span>实时分布</span>
            </div>
            <div className="status-list">
              {Object.entries(overview?.byStatus || {}).map(([status, count]) => (
                <div key={status}>
                  <span className={`dot dot-${status}`} />
                  <span>{statusLabel(status)}</span>
                  <strong>{count}</strong>
                </div>
              ))}
              {Object.keys(overview?.byStatus || {}).length === 0 && <p className="notice">暂无项目</p>}
            </div>
          </div>
        </section>
      </main>

      {creating && (
        <div className="modal-mask" onMouseDown={() => setCreating(false)}>
          <form className="modal" onMouseDown={(event) => event.stopPropagation()} onSubmit={create}>
            <div className="modal-head">
              <div>
                <p className="eyebrow">NEW TENDER</p>
                <h2>新增标书</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setCreating(false)}>×</button>
            </div>
            <label className="field">
              <span>项目名称 *</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} autoFocus placeholder="例如：智慧园区信息化建设项目" />
            </label>
            <div className="field-row">
              <label className="field">
                <span>采购人</span>
                <input value={buyer} onChange={(event) => setBuyer(event.target.value)} placeholder="可选" />
              </label>
              <label className="field">
                <span>预算金额</span>
                <input value={budget} onChange={(event) => setBudget(event.target.value)} placeholder="可选" />
              </label>
            </div>
            <label className="field">
              <span>投标截止</span>
              <input value={deadline} onChange={(event) => setDeadline(event.target.value)} placeholder="例如：2026-12-31" />
            </label>
            {error && <p className="form-error">{error}</p>}
            <div className="modal-actions">
              <button type="button" className="ghost-button" onClick={() => setCreating(false)}>取消</button>
              <button type="submit" className="primary-button" disabled={busy}>
                {busy ? '创建中…' : '创建标书'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
