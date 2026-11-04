import { useEffect, useMemo, useState } from 'react';
import { PipelineScene } from '../components/PipelineScene';
import { UserMenu } from '../components/UserMenu';
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

const STATUS_FLOW = ['intake', 'analyzed', 'outlined', 'drafting', 'review', 'delivery', 'complete'];

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
        </nav>
        <div className="topbar-right">
          <label className="search">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索项目名称、编号或采购人"
            />
          </label>
          <UserMenu account={account} onLogout={onLogout} />
        </div>
      </header>

      <main className="workspace">
        <section className="hero">
          <div className="hero-copy">
            <p className="eyebrow"><i className="eyebrow-rule" />投标工作台 · BID WORKSPACE</p>
            <h1>
              {account.displayName}，今天从
              <br />
              <span className="hero-mark">哪一份标书</span>开始？
            </h1>
            <p className="lead">
              从招标文件到可交付标书，一条工作线走完。上传文件即可解读资格条件、评分办法与风险项，
              再逐章编写、检查并导出。
            </p>
            <ol className="flow-rail">
              <li><span>01</span><strong>招标解读</strong><small>资格 · 评分 · 风险</small></li>
              <li><span>02</span><strong>大纲规划</strong><small>章节结构 · 字数</small></li>
              <li><span>03</span><strong>正文编写</strong><small>逐章生成 · 编辑</small></li>
              <li><span>04</span><strong>质量检查</strong><small>规则 + AI 审核</small></li>
              <li><span>05</span><strong>文档交付</strong><small>DOCX · PDF</small></li>
            </ol>
            <div className="hero-foot">
              <span><i />私有化部署 · 数据不出内网</span>
              <span><i />全程留痕 · 每一处修改有依据</span>
            </div>
          </div>
          <div className="hero-side">
            <div className="hero-visual">
              <span className="visual-tag">BID PIPELINE · 投标流水线</span>
              <PipelineScene />
            </div>
            <div className="scene-caption" aria-hidden="true">
              <span>招标文件</span>
              <i />
              <span>解读评分</span>
              <i />
              <span>标书章节</span>
              <i />
              <span>交付文件</span>
            </div>
            <div className="hero-stats">
              <div className="stat-card tone-indigo">
                <span>项目总数</span>
                <strong>{overview?.total ?? 0}</strong>
                <small>全部投标项目</small>
              </div>
              <div className="stat-card tone-violet">
                <span>进行中</span>
                <strong>{active}</strong>
                <small>正在推进的项目</small>
              </div>
              <div className="stat-card tone-teal">
                <span>已完成</span>
                <strong>{completed}</strong>
                <small>已交付归档</small>
              </div>
              <div className="stat-card tone-cyan">
                <span>资料文件</span>
                <strong>{overview?.files ?? 0}</strong>
                <small>招标与参考资料</small>
              </div>
            </div>
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
                    <span className="sheet-brandline">
                      <i className="sheet-monogram">智</i>
                      <span>
                        <strong>智标</strong>
                        <small>投标文件</small>
                      </span>
                    </span>
                    {index === 0 && query === '' && <span className="sheet-flag">最近增加</span>}
                  </div>
                  <span className="sheet-watermark" aria-hidden="true">ZB</span>
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
            <GrowthChart items={overview?.monthly || []} />
          </div>
          <div className="panel">
            <div className="panel-head">
              <h3>项目状态</h3>
              <span>实时分布</span>
            </div>
            <div className="status-list">
              {STATUS_FLOW.map((status) => {
                const count = overview?.byStatus?.[status] || 0;
                const max = Math.max(1, ...STATUS_FLOW.map((key) => overview?.byStatus?.[key] || 0));
                return (
                  <div className={`status-row ${count === 0 ? 'is-zero' : ''}`} key={status}>
                    <span className={`dot dot-${status}`} />
                    <span className="status-name">{statusLabel(status)}</span>
                    <span className="status-track">
                      <i className={`status-fill fill-${status}`} style={{ width: `${Math.round((count / max) * 100)}%` }} />
                    </span>
                    <strong>{count}</strong>
                  </div>
                );
              })}
            </div>
            <p className="status-foot">
              共 {overview?.total ?? 0} 个项目 · 按工作阶段统计
            </p>
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

function GrowthChart({ items }: { items: { month: string; count: number }[] }) {
  const width = 620;
  const height = 210;
  const pad = { left: 30, right: 22, top: 26, bottom: 34 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const max = Math.max(1, ...items.map((item) => item.count));
  const points = items.map((item, index) => ({
    ...item,
    x: pad.left + (items.length > 1 ? (index * innerW) / (items.length - 1) : innerW / 2),
    y: pad.top + innerH - (item.count / max) * innerH,
  }));
  const line = points
    .map((point, index) => `${index === 0 ? 'M' : 'L'}${point.x.toFixed(1)},${point.y.toFixed(1)}`)
    .join(' ');
  const area = points.length
    ? `M${points[0].x},${pad.top + innerH} L${points
        .map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`)
        .join(' L')} L${points[points.length - 1].x},${pad.top + innerH} Z`
    : '';
  const total = items.reduce((sum, item) => sum + item.count, 0);

  if (!items.length) {
    return <p className="notice">暂无增长数据</p>;
  }

  return (
    <div className="growth-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="近 6 个月项目增长">
        <defs>
          <linearGradient id="growth-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#8b5cf6" stopOpacity="0.26" />
            <stop offset="100%" stopColor="#22d3ee" stopOpacity="0.02" />
          </linearGradient>
          <linearGradient id="growth-line" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor="#8b5cf6" />
            <stop offset="100%" stopColor="#22d3ee" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3].map((index) => (
          <line
            key={index}
            className="chart-grid"
            x1={pad.left}
            x2={width - pad.right}
            y1={pad.top + (innerH * index) / 3}
            y2={pad.top + (innerH * index) / 3}
          />
        ))}
        {area && <path d={area} fill="url(#growth-area)" />}
        {line && <path d={line} fill="none" stroke="url(#growth-line)" strokeWidth="2.4" strokeLinejoin="round" />}
        {points.map((point) => (
          <g key={point.month}>
            <circle cx={point.x} cy={point.y} r={point.count > 0 ? 4.6 : 3.2} fill="#fff" stroke="#6366f1" strokeWidth="1.6" />
            <text className="chart-value" x={point.x} y={point.y - 11} textAnchor="middle">
              {point.count}
            </text>
            <text className="chart-month" x={point.x} y={height - 12} textAnchor="middle">
              {point.month.slice(5)}
            </text>
          </g>
        ))}
      </svg>
      <div className="growth-foot">
        <span>近 6 个月新增</span>
        <strong>{total}</strong>
        <span>份标书</span>
      </div>
    </div>
  );
}
