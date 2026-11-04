import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Backdrop } from '../components/Backdrop';
import {
  api,
  type Account,
  type Analysis,
  type BidDocument,
  type Delivery,
  type FixProposal,
  type Review,
  type Section,
  type Tender,
  type TenderFile,
} from '../api';

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

const ROLE_LABEL: Record<string, string> = {
  tender: '招标文件',
  response: '投标文件',
  template: '模板文件',
  attachment: '参考资料',
};

const SEVERITY_LABEL: Record<string, string> = {
  blocker: '阻断',
  critical: '严重',
  major: '主要',
  minor: '次要',
  info: '提示',
};

const STEPS = [
  { key: 'intake', label: '资料接收' },
  { key: 'analyzed', label: '招标解读' },
  { key: 'outlined', label: '大纲规划' },
  { key: 'drafting', label: '标书编写' },
  { key: 'review', label: '质量检查' },
  { key: 'delivery', label: '文档交付' },
  { key: 'complete', label: '完成' },
];

const STAGE_ORDER = ['intake', 'analyzed', 'outlined', 'drafting', 'review', 'delivery', 'complete'];

function stageIndex(status: string) {
  const index = STAGE_ORDER.indexOf(status);
  return index < 0 ? 0 : index;
}

type Tab = 'overview' | 'files' | 'analysis' | 'writing' | 'review' | 'delivery';

type Props = {
  account: Account;
  onLogout: () => void;
};

export function TenderDetailPage({ account, onLogout }: Props) {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const [tender, setTender] = useState<Tender | null>(null);
  const [files, setFiles] = useState<TenderFile[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [document, setDocument] = useState<BidDocument | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [tab, setTab] = useState<Tab>('overview');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [role, setRole] = useState('tender');
  const [uploading, setUploading] = useState(false);
  const [preview, setPreview] = useState<TenderFile | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState('');
  const [generatingAll, setGeneratingAll] = useState(false);
  const [saving, setSaving] = useState('');
  const [reviewMode, setReviewMode] = useState('mixed');
  const [fixes, setFixes] = useState<Record<string, FixProposal>>({});
  const [fixing, setFixing] = useState('');
  const [applying, setApplying] = useState('');
  const [deliveryFormat, setDeliveryFormat] = useState('docx');
  const [delivering, setDelivering] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const load = async () => {
    setError('');
    try {
      const [detail, fileList] = await Promise.all([api.tender(id), api.files(id)]);
      setTender(detail);
      setFiles(fileList.items);

      const [analysisResult, documentResult, reviewResult, deliveryResult] =
        await Promise.allSettled([
          api.analysis(id),
          api.document(id),
          detail.reviewId ? api.review(detail.reviewId) : Promise.reject(new Error('无检查记录')),
          api.deliveries(id),
        ]);
      setAnalysis(analysisResult.status === 'fulfilled' ? analysisResult.value : null);
      setDocument(documentResult.status === 'fulfilled' ? documentResult.value : null);
      setReview(reviewResult.status === 'fulfilled' ? reviewResult.value : null);
      setDeliveries(deliveryResult.status === 'fulfilled' ? deliveryResult.value.items : []);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '加载失败');
    }
  };

  useEffect(() => {
    load();
  }, [id]);

  useEffect(() => {
    if (!document) {
      setDrafts({});
      return;
    }
    const next: Record<string, string> = {};
    for (const row of document.sections) next[row.id] = row.body;
    setDrafts(next);
    setFixes({});
  }, [document]);

  const upload = async (picked: File | null) => {
    if (!picked) return;
    setUploading(true);
    setError('');
    try {
      await api.uploadFile(id, picked, role);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '上传失败');
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  const removeFile = async (fileId: string) => {
    if (!window.confirm('确定删除这个文件？')) return;
    try {
      await api.deleteFile(id, fileId);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '删除失败');
    }
  };

  const removeTender = async () => {
    if (!window.confirm('删除项目会同时移除文件与检查记录，确定继续？')) return;
    try {
      await api.deleteTender(id);
      navigate('/');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '删除失败');
    }
  };

  const openPreview = async (file: TenderFile) => {
    try {
      const full = await api.fileText(id, file.id);
      setPreview(full);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '读取失败');
    }
  };

  const runAnalysis = async () => {
    if (busy) return;
    setBusy('analysis');
    setError('');
    try {
      const result = await api.runAnalysis(id);
      await load();
      setTab('analysis');
      setAnalysis(result);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '解读失败');
    } finally {
      setBusy('');
    }
  };

  const buildOutline = async () => {
    if (busy) return;
    setBusy('outline');
    setError('');
    try {
      await api.buildOutline(id);
      await load();
      setTab('writing');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '大纲生成失败');
    } finally {
      setBusy('');
    }
  };

  const generateSection = async (row: Section) => {
    if (generating) return false;
    setGenerating(row.id);
    setError('');
    setDrafts((prev) => ({ ...prev, [row.id]: '' }));
    try {
      await api.streamSection(
        id,
        row.id,
        (text) => setDrafts((prev) => ({ ...prev, [row.id]: (prev[row.id] || '') + text })),
        (section) => {
          setDrafts((prev) => ({ ...prev, [row.id]: section.body }));
          setDocument((prev) =>
            prev
              ? {
                  ...prev,
                  sections: prev.sections.map((item) => (item.id === section.id ? section : item)),
                }
              : prev,
          );
        },
      );
      return true;
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '生成失败');
      return false;
    } finally {
      setGenerating('');
    }
  };

  const generateAll = async () => {
    if (!document || generatingAll) return;
    setGeneratingAll(true);
    setError('');
    for (const row of document.sections) {
      const written = (drafts[row.id] ?? row.body ?? '').trim();
      if (written) continue;
      const ok = await generateSection(row);
      if (!ok) break;
    }
    setGeneratingAll(false);
    await load();
  };

  const saveSection = async (row: Section) => {
    setSaving(row.id);
    setError('');
    try {
      const body = drafts[row.id] ?? '';
      const updated = await api.patchSection(id, row.id, { body });
      setDocument((prev) =>
        prev
          ? { ...prev, sections: prev.sections.map((item) => (item.id === row.id ? updated : item)) }
          : prev,
      );
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '保存失败');
    } finally {
      setSaving('');
    }
  };

  const runReview = async () => {
    if (busy) return;
    setBusy('review');
    setError('');
    try {
      const result = await api.runReview(id, reviewMode);
      setReview(result);
      await load();
      setTab('review');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '检查失败');
    } finally {
      setBusy('');
    }
  };

  const requestFix = async (issueId: string) => {
    if (!review || fixing) return;
    setFixing(issueId);
    setError('');
    try {
      const proposal = await api.proposeFix(review.id, issueId);
      setFixes((prev) => ({ ...prev, [issueId]: proposal }));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '修订建议生成失败');
    } finally {
      setFixing('');
    }
  };

  const applyFix = async (issueId: string) => {
    const proposal = fixes[issueId];
    if (!review || !proposal) return;
    setApplying(issueId);
    setError('');
    try {
      await api.applyFix(review.id, issueId, proposal.id);
      const refreshed = await api.review(review.id);
      setReview(refreshed);
      const doc = await api.document(id);
      setDocument(doc);
      setFixes((prev) => {
        const next = { ...prev };
        delete next[issueId];
        return next;
      });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '应用修订失败');
    } finally {
      setApplying('');
    }
  };

  const ignoreIssue = async (issueId: string) => {
    if (!review) return;
    try {
      await api.patchIssue(review.id, issueId, 'ignored');
      const refreshed = await api.review(review.id);
      setReview(refreshed);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '操作失败');
    }
  };

  const createDelivery = async () => {
    if (delivering) return;
    setDelivering(true);
    setError('');
    try {
      await api.createDelivery(id, deliveryFormat);
      const result = await api.deliveries(id);
      setDeliveries(result.items);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '交付文件生成失败');
    } finally {
      setDelivering(false);
    }
  };

  const current = tender ? stageIndex(tender.status) : 0;
  const writtenCount = useMemo(
    () => (document ? document.sections.filter((row) => (drafts[row.id] ?? row.body).trim()).length : 0),
    [document, drafts],
  );
  const canWrite = account.permissions.includes('document.write');
  const canReview = account.permissions.includes('review.run');
  const canDeliver = account.permissions.includes('delivery.run');
  const canAnalyze = account.permissions.includes('analysis.run');

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brand-mark">智</span>
          <span className="brand-copy"><strong>智标</strong><small>BID INTELLIGENCE</small></span>
        </a>
        <nav className="topnav">
          <a href="/">工作台</a>
          <a className="active" href={`/tender/${id}`}>投标项目</a>
          {(account.isSuperuser || account.permissions.includes('admin.ai')) && <a href="/settings/ai">模型服务</a>}
          {account.permissions.includes('admin.accounts') && <a href="/settings/accounts">账号权限</a>}
        </nav>
        <div className="topbar-right">
          <span className="account-chip">{account.displayName.slice(0, 1)}</span>
          <button className="ghost-button" onClick={onLogout}>退出</button>
        </div>
      </header>

      <main className="workspace">
        <section className="tender-hero">
          <Backdrop dense />
          <div className="hero-copy">
            <p className="eyebrow">{tender?.code || 'TENDER'}</p>
            <h1>{tender?.title || '正在加载…'}</h1>
            <div className="tender-meta">
              <span className={`status status-${tender?.status || 'intake'}`}>
                {STATUS_LABEL[tender?.status || ''] || '资料接收'}
              </span>
              {tender?.buyer && <span>采购人 · {tender.buyer}</span>}
              {tender?.budget && <span>预算 · {tender.budget}</span>}
              {tender?.deadline && <span>截止 · {tender.deadline}</span>}
            </div>
          </div>
          <div className="tender-actions">
            <button className="ghost-button danger" onClick={removeTender}>删除项目</button>
          </div>
        </section>

        <nav className="steps">
          {STEPS.map((step, index) => (
            <div key={step.key} className={`step ${index <= current ? 'done' : ''} ${index === current ? 'current' : ''}`}>
              <span>{String(index + 1).padStart(2, '0')}</span>
              <strong>{step.label}</strong>
            </div>
          ))}
        </nav>

        {error && <p className="notice error">{error}</p>}

        <section className="tabs">
          <div className="tab-bar">
            <button className={tab === 'overview' ? 'active' : ''} onClick={() => setTab('overview')}>概览</button>
            <button className={tab === 'files' ? 'active' : ''} onClick={() => setTab('files')}>
              资料文件 <span>{files.length}</span>
            </button>
            <button className={tab === 'analysis' ? 'active' : ''} onClick={() => setTab('analysis')}>
              招标解读 <span>{analysis ? '已完成' : '待开始'}</span>
            </button>
            <button className={tab === 'writing' ? 'active' : ''} onClick={() => setTab('writing')}>
              标书编写 <span>{document ? `${writtenCount}/${document.sections.length}` : '待开始'}</span>
            </button>
            <button className={tab === 'review' ? 'active' : ''} onClick={() => setTab('review')}>
              质量检查 <span>{review ? `${review.score} 分` : '待开始'}</span>
            </button>
            <button className={tab === 'delivery' ? 'active' : ''} onClick={() => setTab('delivery')}>
              文档交付 <span>{deliveries.length}</span>
            </button>
          </div>

          {tab === 'overview' && (
            <div className="detail-grid">
              <article className="info-card">
                <h3>项目信息</h3>
                <dl>
                  <div><dt>项目编号</dt><dd>{tender?.code || '—'}</dd></div>
                  <div><dt>采购人</dt><dd>{tender?.buyer || '未填写'}</dd></div>
                  <div><dt>预算金额</dt><dd>{tender?.budget || '未填写'}</dd></div>
                  <div><dt>投标截止</dt><dd>{tender?.deadline || '未填写'}</dd></div>
                  <div><dt>创建时间</dt><dd>{tender?.createdAt?.slice(0, 16).replace('T', ' ') || '—'}</dd></div>
                </dl>
              </article>
              <article className="info-card">
                <h3>工作进度</h3>
                <ul className="progress-list">
                  <li><span>招标资料</span><strong>{files.length} 份</strong></li>
                  <li><span>招标解读</span><strong>{analysis ? '已完成' : '待开始'}</strong></li>
                  <li><span>标书大纲</span><strong>{document ? `${document.sections.length} 节` : '待开始'}</strong></li>
                  <li><span>正文完成</span><strong>{document ? `${writtenCount}/${document.sections.length}` : '0/0'}</strong></li>
                  <li><span>质量检查</span><strong>{review ? `${review.score} 分` : '待开始'}</strong></li>
                  <li><span>交付文件</span><strong>{deliveries.length} 个</strong></li>
                </ul>
              </article>
              <article className="info-card wide">
                <h3>下一步</h3>
                <p>
                  {!files.length
                    ? '先上传招标文件（PDF / DOCX / TXT），系统会解析正文用于解读。'
                    : !analysis
                      ? '招标文件已解析完成，可以执行招标解读，提炼资格条件、评分办法与风险项。'
                      : !document
                        ? '解读已完成，可以生成标书大纲，把评分项落到章节结构上。'
                        : writtenCount < document.sections.length
                          ? `还有 ${document.sections.length - writtenCount} 个章节未完成，进入标书编写继续生成正文。`
                          : !review
                            ? '正文已就绪，可以执行质量检查，发现问题后逐条修订。'
                            : '可以直接生成 DOCX 或 PDF 交付文件。'}
                </p>
                {!files.length ? (
                  <button className="primary-button" onClick={() => setTab('files')}>管理资料文件</button>
                ) : !analysis ? (
                  <button className="primary-button" onClick={runAnalysis} disabled={!!busy || !canAnalyze}>
                    {busy === 'analysis' ? '解读中…' : '开始招标解读'}
                  </button>
                ) : !document ? (
                  <button className="primary-button" onClick={buildOutline} disabled={!!busy || !canWrite}>
                    {busy === 'outline' ? '生成中…' : '生成标书大纲'}
                  </button>
                ) : (
                  <button className="primary-button" onClick={() => setTab('writing')}>进入标书编写</button>
                )}
              </article>
            </div>
          )}

          {tab === 'files' && (
            <div className="files-panel">
              <div className="upload-box">
                <div>
                  <h3>上传资料</h3>
                  <p>支持 PDF、DOCX、TXT、MD、CSV，单文件不超过 80MB。</p>
                </div>
                <div className="upload-controls">
                  <select value={role} onChange={(event) => setRole(event.target.value)}>
                    {Object.entries(ROLE_LABEL).map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                  <input
                    ref={inputRef}
                    type="file"
                    accept=".pdf,.docx,.txt,.md,.csv"
                    hidden
                    onChange={(event) => upload(event.target.files?.[0] || null)}
                  />
                  <button className="primary-button" onClick={() => inputRef.current?.click()} disabled={uploading}>
                    {uploading ? '上传中…' : '选择文件'}
                  </button>
                </div>
              </div>

              {files.length === 0 ? (
                <p className="notice">还没有上传资料文件。</p>
              ) : (
                <div className="file-list">
                  {files.map((file) => (
                    <div className="file-row" key={file.id}>
                      <span className="file-icon">{file.filename.split('.').pop()?.toUpperCase().slice(0, 4)}</span>
                      <div className="file-main">
                        <strong>{file.filename}</strong>
                        <span>{ROLE_LABEL[file.role] || file.role} · {Math.round(file.size / 1024)} KB · {file.parseState}</span>
                      </div>
                      <div className="file-actions">
                        <button className="ghost-button" onClick={() => openPreview(file)}>预览</button>
                        <button className="ghost-button danger" onClick={() => removeFile(file.id)}>删除</button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {tab === 'analysis' && (
            <div className="analysis-panel">
              {!analysis ? (
                <div className="empty-panel">
                  <p className="eyebrow">ANALYSIS</p>
                  <h3>还没有招标解读</h3>
                  <p>系统会读取招标文件正文，提炼项目概况、资格条件、评分办法与风险项。</p>
                  <button className="primary-button" onClick={runAnalysis} disabled={!!busy}>
                    {busy === 'analysis' ? '解读中，请稍候…' : '开始招标解读'}
                  </button>
                </div>
              ) : (
                <>
                  <div className="panel-head">
                    <div>
                      <p className="eyebrow">第 {analysis.revision} 版 · {analysis.model || '默认模型'}</p>
                      <h2>招标解读</h2>
                    </div>
                    <div className="panel-actions">
                      <button className="ghost-button" onClick={runAnalysis} disabled={!!busy}>
                        {busy === 'analysis' ? '重新解读中…' : '重新解读'}
                      </button>
                      <button className="primary-button" onClick={buildOutline} disabled={!!busy || !canWrite}>
                        {busy === 'outline' ? '生成中…' : document ? '重建大纲' : '生成标书大纲'}
                      </button>
                    </div>
                  </div>

                  <article className="analysis-overview">
                    <h3>项目总览</h3>
                    <p>{analysis.overview || '模型未返回总览。'}</p>
                  </article>

                  <div className="dimension-grid">
                    {analysis.dimensions.map((item) => (
                      <div className="dimension-card" key={item.key || item.label}>
                        <span>{item.label}</span>
                        <strong>{item.value || '未明确'}</strong>
                        {item.note && <small>{item.note}</small>}
                      </div>
                    ))}
                  </div>

                  {(analysis.scoring?.items?.length ?? 0) > 0 && (
                    <article className="info-card wide">
                      <h3>评分办法 {analysis.scoring.method && <small>{analysis.scoring.method}</small>}</h3>
                      <table className="scoring-table">
                        <thead>
                          <tr><th>评分项</th><th>分值</th><th>得分条件</th></tr>
                        </thead>
                        <tbody>
                          {analysis.scoring.items.map((item) => (
                            <tr key={item.name}>
                              <td>{item.name}</td>
                              <td>{item.points}</td>
                              <td>{item.criteria}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </article>
                  )}

                  {analysis.requirements.length > 0 && (
                    <article className="info-card wide">
                      <h3>实质性要求</h3>
                      <ul className="requirement-list">
                        {analysis.requirements.map((item) => <li key={item}>{item}</li>)}
                      </ul>
                    </article>
                  )}

                  {analysis.risks.length > 0 && (
                    <article className="info-card wide">
                      <h3>风险提示</h3>
                      <ul className="risk-list">
                        {analysis.risks.map((risk, index) => (
                          <li key={`${risk.title}-${index}`} className={`risk sev-${risk.level}`}>
                            <span className="sev-tag">{SEVERITY_LABEL[risk.level] || risk.level}</span>
                            <div>
                              <strong>{risk.title}</strong>
                              <p>{risk.detail}</p>
                            </div>
                          </li>
                        ))}
                      </ul>
                    </article>
                  )}
                </>
              )}
            </div>
          )}

          {tab === 'writing' && (
            <div className="writing-panel">
              {!document ? (
                <div className="empty-panel">
                  <p className="eyebrow">DOCUMENT</p>
                  <h3>还没有标书大纲</h3>
                  <p>{analysis ? '解读已完成，可以生成章节结构后逐章编写。' : '请先完成招标解读，再生成标书大纲。'}</p>
                  {analysis ? (
                    <button className="primary-button" onClick={buildOutline} disabled={!!busy || !canWrite}>
                      {busy === 'outline' ? '生成中…' : '生成标书大纲'}
                    </button>
                  ) : (
                    <button className="primary-button" onClick={() => setTab('analysis')}>前往招标解读</button>
                  )}
                </div>
              ) : (
                <>
                  <div className="panel-head">
                    <div>
                      <p className="eyebrow">{document.title}</p>
                      <h2>标书编写 <small>{writtenCount}/{document.sections.length} 节已完成</small></h2>
                    </div>
                    <div className="panel-actions">
                      <button className="ghost-button" onClick={buildOutline} disabled={!!busy || !canWrite}>
                        {busy === 'outline' ? '重建中…' : '重建大纲'}
                      </button>
                      <button className="primary-button" onClick={generateAll} disabled={generatingAll || !canWrite}>
                        {generatingAll ? '正在逐章生成…' : '生成全部未完成章节'}
                      </button>
                    </div>
                  </div>

                  <div className="section-list">
                    {document.sections.map((row) => {
                      const top = !row.parentKey;
                      const active = generating === row.id;
                      return (
                        <article className={`section-card ${top ? 'top' : 'child'}`} key={row.id}>
                          <header className="section-head">
                            <div>
                              <span className="section-index">{String(row.position + 1).padStart(2, '0')}</span>
                              <strong>{row.heading}</strong>
                              <small>计划 {row.targetWords} 字 · {row.status === 'written' ? '已编写' : '待编写'}</small>
                            </div>
                            <div className="section-actions">
                              <button
                                className="ghost-button"
                                onClick={() => generateSection(row)}
                                disabled={!!generating || !canWrite}
                              >
                                {active ? '生成中…' : (drafts[row.id] || row.body).trim() ? '重新生成' : 'AI 编写'}
                              </button>
                              <button
                                className="primary-button small"
                                onClick={() => saveSection(row)}
                                disabled={saving === row.id || !canWrite}
                              >
                                {saving === row.id ? '保存中…' : '保存'}
                              </button>
                            </div>
                          </header>
                          {row.intent && <p className="section-intent">写作要点：{row.intent}</p>}
                          <textarea
                            className="section-editor"
                            value={drafts[row.id] ?? ''}
                            placeholder={active ? '正在生成正文…' : '点击“AI 编写”生成，或直接输入正文'}
                            onChange={(event) => setDrafts((prev) => ({ ...prev, [row.id]: event.target.value }))}
                            rows={top ? 8 : 5}
                            readOnly={active}
                          />
                        </article>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          )}

          {tab === 'review' && (
            <div className="review-panel">
              <div className="panel-head">
                <div>
                  <p className="eyebrow">QUALITY REVIEW</p>
                  <h2>质量检查</h2>
                </div>
                <div className="panel-actions">
                  <select value={reviewMode} onChange={(event) => setReviewMode(event.target.value)}>
                    <option value="mixed">规则 + AI</option>
                    <option value="rule">仅规则</option>
                    <option value="ai">仅 AI</option>
                  </select>
                  <button className="primary-button" onClick={runReview} disabled={!!busy || !canReview || !document}>
                    {busy === 'review' ? '检查中…' : review ? '重新检查' : '执行质量检查'}
                  </button>
                </div>
              </div>

              {!document && <p className="notice">请先生成标书大纲并编写正文，再执行质量检查。</p>}

              {review && (
                <>
                  <div className="review-score">
                    <div className="score-ring">
                      <strong>{review.score}</strong>
                      <span>综合得分</span>
                    </div>
                    <div className="score-copy">
                      <h3>{review.verdict || '检查完成'}</h3>
                      <p>{review.summary}</p>
                      <small>检查方式：{review.mode === 'mixed' ? '规则 + AI' : review.mode === 'rule' ? '规则' : 'AI'} · {review.createdAt?.slice(0, 16).replace('T', ' ')}</small>
                    </div>
                  </div>

                  {(review.issues?.length ?? 0) === 0 ? (
                    <p className="notice">没有发现问题，可以进入文档交付。</p>
                  ) : (
                    <div className="issue-list">
                      {review.issues?.map((issue) => {
                        const proposal = fixes[issue.id];
                        return (
                          <article className={`issue-card sev-${issue.severity}`} key={issue.id}>
                            <header>
                              <span className="sev-tag">{SEVERITY_LABEL[issue.severity] || issue.severity}</span>
                              <strong>{issue.category} · {issue.location || '全文'}</strong>
                              <small>{issue.status === 'resolved' ? '已修订' : issue.status === 'ignored' ? '已忽略' : '待处理'}</small>
                            </header>
                            <p>{issue.finding}</p>
                            <p className="issue-advice">建议：{issue.recommendation || '—'}</p>
                            {issue.status === 'open' && (
                              <div className="issue-actions">
                                {!proposal && (
                                  <button
                                    className="ghost-button"
                                    onClick={() => requestFix(issue.id)}
                                    disabled={fixing === issue.id || !canWrite}
                                  >
                                    {fixing === issue.id ? '正在生成修订…' : '生成修订建议'}
                                  </button>
                                )}
                                {proposal && (
                                  <>
                                    <button
                                      className="primary-button small"
                                      onClick={() => applyFix(issue.id)}
                                      disabled={applying === issue.id || !canWrite}
                                    >
                                      {applying === issue.id ? '应用中…' : '应用修订'}
                                    </button>
                                    <button
                                      className="ghost-button"
                                      onClick={() => setFixes((prev) => {
                                        const next = { ...prev };
                                        delete next[issue.id];
                                        return next;
                                      })}
                                    >
                                      放弃建议
                                    </button>
                                  </>
                                )}
                                <button className="ghost-button danger" onClick={() => ignoreIssue(issue.id)}>
                                  忽略问题
                                </button>
                              </div>
                            )}
                            {proposal && (
                              <div className="proposal">
                                <p className="eyebrow">修订预览 · {proposal.sectionHeading}</p>
                                <pre>{proposal.proposedText}</pre>
                              </div>
                            )}
                          </article>
                        );
                      })}
                    </div>
                  )}
                </>
              )}

              {!review && document && (
                <div className="empty-panel">
                  <p className="eyebrow">REVIEW</p>
                  <h3>还没有执行检查</h3>
                  <p>规则检查覆盖章节完整性、字数与关键响应点，AI 检查模拟评标视角审查响应性。</p>
                  <button className="primary-button" onClick={runReview} disabled={!!busy || !canReview}>
                    {busy === 'review' ? '检查中…' : '执行质量检查'}
                  </button>
                </div>
              )}
            </div>
          )}

          {tab === 'delivery' && (
            <div className="delivery-panel">
              <div className="panel-head">
                <div>
                  <p className="eyebrow">DELIVERY</p>
                  <h2>文档交付</h2>
                </div>
                <div className="panel-actions">
                  <div className="segmented">
                    <button className={deliveryFormat === 'docx' ? 'active' : ''} onClick={() => setDeliveryFormat('docx')}>DOCX</button>
                    <button className={deliveryFormat === 'pdf' ? 'active' : ''} onClick={() => setDeliveryFormat('pdf')}>PDF</button>
                  </div>
                  <button className="primary-button" onClick={createDelivery} disabled={delivering || !canDeliver || !document}>
                    {delivering ? '生成中…' : `生成 ${deliveryFormat.toUpperCase()}`}
                  </button>
                </div>
              </div>

              {!document && <p className="notice">请先生成标书大纲并编写正文。</p>}

              {deliveries.length === 0 ? (
                <p className="notice">还没有交付文件。</p>
              ) : (
                <div className="delivery-list">
                  {deliveries.map((item) => (
                    <div className="delivery-row" key={item.id}>
                      <span className={`format-chip ${item.format}`}>{item.format.toUpperCase()}</span>
                      <div>
                        <strong>{item.filename}</strong>
                        <span>{Math.round(item.size / 1024)} KB · {item.createdAt?.slice(0, 16).replace('T', ' ')}</span>
                      </div>
                      <a className="ghost-button" href={item.downloadUrl}>下载</a>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>
      </main>

      {preview && (
        <div className="modal-mask" onMouseDown={() => setPreview(null)}>
          <div className="modal preview" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <div><p className="eyebrow">PARSED TEXT</p><h2>{preview.filename}</h2></div>
              <button className="icon-button" onClick={() => setPreview(null)}>×</button>
            </div>
            <pre>{preview.text || '未解析出文本'}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
