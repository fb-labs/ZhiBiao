import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { WorkspaceScene } from '../components/AmbientScene';
import { UserMenu } from '../components/UserMenu';
import {
  api,
  type Account,
  type Analysis,
  type BidDocument,
  type Delivery,
  type FixProposal,
  type GenerationIssue,
  type GenerationJob,
  type QualityReport,
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

const SECTION_STATE: Record<string, string> = {
  written: '已编写',
  failed: '失败',
  pending: '待编写',
};

const CN_DIGITS: Record<string, number> = {
  一: 1,
  二: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  七: 7,
  八: 8,
  九: 9,
};

function cnNumber(title: string): number | null {
  const match = title.match(/^\s*([一二三四五六七八九十]{1,3})、/);
  if (!match) return null;
  const raw = match[1];
  if (raw === '十') return 10;
  if (raw.startsWith('十')) return 10 + (CN_DIGITS[raw[1]] || 0);
  if (raw.includes('十')) {
    const [head, tail] = raw.split('十');
    return (CN_DIGITS[head] || 0) * 10 + (tail ? CN_DIGITS[tail] || 0 : 0);
  }
  return CN_DIGITS[raw] ?? null;
}

function sectionNumbers(sections: Section[]): Record<string, string> {
  // 与后端 chapter_rules.section_numbers 同一口径：按层级与出现顺序计算 1 / 1.1 / 2 / 2.1；
  // 招标原件自带「一、」「十二、」时以原标题序号为准。
  const counters: number[] = [];
  const numbers: Record<string, string> = {};
  for (const row of sections) {
    const depth = row.parentKey ? 2 : 1;
    if (depth === 1) {
      const named = cnNumber(row.heading);
      if (named !== null) {
        counters.length = 0;
        counters.push(named);
        numbers[row.id] = String(named);
        continue;
      }
    }
    if (counters.length < depth) {
      for (let extra = counters.length; extra < depth; extra += 1) counters.push(1);
    } else {
      counters.length = depth;
      counters[depth - 1] += 1;
    }
    numbers[row.id] = counters.join('.');
  }
  return numbers;
}

const SEVERITY_LABEL: Record<string, string> = {
  blocker: '阻断',
  critical: '严重',
  major: '主要',
  minor: '次要',
  info: '提示',
};

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
  const [activeSectionId, setActiveSectionId] = useState('');
  const [generating, setGenerating] = useState('');
  const [saving, setSaving] = useState('');
  const [manualEdit, setManualEdit] = useState(false);
  const [sectionScores, setSectionScores] = useState<Record<string, string[]>>({});
  const [outlineInfo, setOutlineInfo] = useState<{ mode?: string; note?: string; targetWords?: number } | null>(null);
  const [outlineMode, setOutlineMode] = useState('score');
  const [targetWords, setTargetWords] = useState(80000);
  const [generationJob, setGenerationJob] = useState<GenerationJob | null>(null);
  const [qualityReport, setQualityReport] = useState<QualityReport | null>(null);
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

      const [
        analysisResult,
        documentResult,
        reviewResult,
        deliveryResult,
        outlineResult,
        generationResult,
      ] = await Promise.allSettled([
        api.analysis(id),
        api.document(id),
        detail.reviewId ? api.review(detail.reviewId) : Promise.reject(new Error('无检查记录')),
        api.deliveries(id),
        api.outline(id),
        api.generationState(id),
      ]);
      setAnalysis(analysisResult.status === 'fulfilled' ? analysisResult.value : null);
      setDocument(documentResult.status === 'fulfilled' ? documentResult.value : null);
      setReview(reviewResult.status === 'fulfilled' ? reviewResult.value : null);
      setDeliveries(deliveryResult.status === 'fulfilled' ? deliveryResult.value.items : []);
      if (outlineResult.status === 'fulfilled') {
        const info = outlineResult.value;
        setOutlineInfo({ mode: info.mode, note: info.note, targetWords: info.targetWords });
        if (info.mode) setOutlineMode(info.mode);
        if (info.targetWords) setTargetWords(info.targetWords);
        const nextScores: Record<string, string[]> = {};
        for (const row of info.sections) nextScores[row.id] = row.scores || [];
        setSectionScores(nextScores);
      }
      if (generationResult.status === 'fulfilled') {
        const state = generationResult.value;
        setGenerationJob(state.active || state.items[0] || null);
      }
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

  useEffect(() => {
    if (!document) {
      setActiveSectionId('');
      return;
    }
    if (!document.sections.some((row) => row.id === activeSectionId)) {
      setActiveSectionId(document.sections[0]?.id || '');
    }
  }, [document, activeSectionId]);

  useEffect(() => {
    setManualEdit(false);
  }, [activeSectionId]);

  useEffect(() => {
    if (!generationJob || !['queued', 'running'].includes(generationJob.status)) return;
    let disposed = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await api.generation(generationJob.id);
        if (disposed) return;
        setGenerationJob(next);
        if (!['queued', 'running'].includes(next.status)) {
          window.clearInterval(timer);
          await load();
        }
      } catch {
        // ignore transient polling errors
      }
    }, 1500);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [generationJob?.id, generationJob?.status]);

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
      const result = await api.buildOutline(id, { mode: outlineMode, targetWords });
      setOutlineInfo({ mode: result.mode, note: result.note, targetWords: result.targetWords });
      setQualityReport(null);
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
    setActiveSectionId(row.id);
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

  const startGeneration = async (mode: 'missing' | 'retry' | 'all') => {
    if (!document || busy || generationJob?.status === 'running') return;
    if (mode === 'all' && !window.confirm('全部重跑会覆盖所有已生成章节，确定继续？')) return;
    setBusy('generation');
    setError('');
    setQualityReport(null);
    try {
      const job = await api.startGeneration(id, { mode });
      setGenerationJob(job);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '启动生成失败');
    } finally {
      setBusy('');
    }
  };

  const cancelGeneration = async () => {
    if (!generationJob) return;
    try {
      await api.cancelGeneration(generationJob.id);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '取消失败');
    }
  };

  const runPostprocess = async () => {
    if (!document) return;
    setBusy('postprocess');
    setError('');
    try {
      const report = await api.postprocess(id);
      setQualityReport(report);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '全文校验失败');
    } finally {
      setBusy('');
    }
  };

  const addSection = async () => {
    const heading = window.prompt('新增章节标题');
    if (!heading?.trim()) return;
    const wordsInput = window.prompt('目标字数', '800');
    const words = Number(wordsInput || 800);
    try {
      await api.addSection(id, {
        heading: heading.trim(),
        targetWords: Number.isFinite(words) ? Math.max(100, Math.min(8000, words)) : 800,
      });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '新增章节失败');
    }
  };

  const removeSection = async (row: Section) => {
    if (!window.confirm(`删除章节「${row.heading}」？`)) return;
    try {
      await api.deleteSection(id, row.id);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '删除章节失败');
    }
  };

  const moveSection = async (row: Section, direction: 'up' | 'down') => {
    try {
      await api.moveSection(id, row.id, direction);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '移动章节失败');
    }
  };

  const saveSection = async (row: Section) => {
    setSaving(row.id);
    setError('');
    try {
      const body = drafts[row.id] ?? row.body;
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

  const writtenCount = useMemo(
    () => (document ? document.sections.filter((row) => (drafts[row.id] ?? row.body).trim()).length : 0),
    [document, drafts],
  );
  const totalWords = useMemo(
    () =>
      document
        ? document.sections.reduce(
            (sum, row) => sum + (drafts[row.id] ?? row.body).replace(/\s/g, '').length,
            0,
          )
        : 0,
    [document, drafts],
  );
  const numbers = useMemo(() => sectionNumbers(document?.sections || []), [document]);
  const activeSection = document?.sections.find((row) => row.id === activeSectionId) || null;
  const activeBody = activeSection ? drafts[activeSection.id] ?? activeSection.body : '';
  const activeDirty = activeSection ? activeBody !== activeSection.body : false;
  const activeWords = activeBody.replace(/\s/g, '').length;
  // 评分对齐：把评分项反向映射到当前目录里的承载章节，正文生成前用于核对覆盖。
  const scoreCoverage = useMemo(() => {
    const items = analysis?.scoring?.items || [];
    const rows = items.map((item) => ({
      name: item.name,
      points: Number(item.points) || 0,
      criteria: item.criteria,
      chapters: (document?.sections || []).filter((row) =>
        (sectionScores[row.id] || []).includes(item.name),
      ),
    }));
    const total = rows.reduce((sum, row) => sum + row.points, 0);
    const covered = rows.reduce((sum, row) => sum + (row.chapters.length ? row.points : 0), 0);
    return {
      rows,
      total,
      covered,
      pct: total ? Math.round((covered / total) * 100) : 0,
      uncovered: rows.filter((row) => !row.chapters.length),
    };
  }, [analysis, document, sectionScores]);
  // 正文还没生成的章节先展示「对齐评分」，让用户确认章节与评分项的关系；
  // 一旦本章有正文（或正在流式生成），右侧就切回正文编辑器。
  const showScore = !!activeSection && !activeBody.trim() && !manualEdit && generating !== activeSection.id;
  const canWrite = account.permissions.includes('document.write');
  const canReview = account.permissions.includes('review.run');
  const canDeliver = account.permissions.includes('delivery.run');
  const canAnalyze = account.permissions.includes('analysis.run');
  const batchRunning = ['queued', 'running'].includes(generationJob?.status || '');

  const switchSection = (nextId: string) => {
    if (!document || nextId === activeSectionId) return;
    if (activeSection && activeDirty) {
      if (!window.confirm('当前章节有未保存的修改，切换章节会丢弃这些修改。确定切换？')) return;
      setDrafts((prev) => ({ ...prev, [activeSection.id]: activeSection.body }));
    }
    setActiveSectionId(nextId);
  };

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brand-mark"><img src="/logo-tile.png" alt="智标" /></span>
          <span className="brand-copy"><strong>智标</strong><small>BID INTELLIGENCE</small></span>
        </a>
        <nav className="topnav">
          <a href="/">工作台</a>
          <a className="active" href={`/tender/${id}`}>投标项目</a>
        </nav>
        <div className="topbar-right">
          <UserMenu account={account} onLogout={onLogout} />
        </div>
      </header>

      <main className="workspace workspace-wide">
        <section className="tender-hero">
          <WorkspaceScene
            step={tab}
            progress={document ? writtenCount / Math.max(1, document.sections.length) : 0}
          />
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

        {error && <p className="notice error">{error}</p>}

        <section className="tabs">
          <div className="tab-bar">
            <button className={`util ${tab === 'overview' ? 'active' : ''}`} onClick={() => setTab('overview')}>概览</button>
            <button className={`util ${tab === 'files' ? 'active' : ''}`} onClick={() => setTab('files')}>
              资料文件 <span>{files.length}</span>
            </button>
            <i className="tab-sep" />
            <button className={tab === 'analysis' ? 'active' : ''} onClick={() => setTab('analysis')}>
              <span className="tab-step">01</span>招标解读<small>{analysis ? '已完成' : '待开始'}</small>
            </button>
            <button className={tab === 'writing' ? 'active' : ''} onClick={() => setTab('writing')}>
              <span className="tab-step">02</span>大纲与编写<small>{document ? `${writtenCount}/${document.sections.length}` : '待开始'}</small>
            </button>
            <button className={tab === 'review' ? 'active' : ''} onClick={() => setTab('review')}>
              <span className="tab-step">03</span>质量检查<small>{review ? `${review.score} 分` : '待开始'}</small>
            </button>
            <button className={tab === 'delivery' ? 'active' : ''} onClick={() => setTab('delivery')}>
              <span className="tab-step">04</span>文档交付<small>{deliveries.length} 个</small>
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
                  <p className="eyebrow">ANALYSIS · 招标解析 Agent</p>
                  <h3>还没有招标解读</h3>
                  <p>招标解析 Agent 会读取招标文件正文，抽取事实台账，并提炼项目概况、资格条件、评分办法与风险项。</p>
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
                  <p className="eyebrow">DOCUMENT · 生成规划 / 正文编制 Agent</p>
                  <h3>还没有标书大纲</h3>
                  <p>{analysis ? '生成规划 Agent 会先识别模板并生成章节结构，再由正文编制 Agent 逐章写作。' : '请先完成招标解读，再生成标书大纲。'}</p>
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
                      <h2>标书编写 <small>{writtenCount}/{document.sections.length} 节已完成 · 约 {totalWords} 字</small></h2>
                      {outlineInfo?.note && (
                        <p className="panel-lead">
                          {outlineInfo.note} · 目标篇幅约 {Math.round(targetWords / 10000)} 万字
                        </p>
                      )}
                    </div>
                  </div>

                  <div className="generation-bar">
                    <div className="gen-settings">
                      <label>
                        结构模式
                        <select value={outlineMode} onChange={(event) => setOutlineMode(event.target.value)} disabled={!!busy}>
                          <option value="score">按评分项对齐</option>
                          <option value="standard">标准投标结构</option>
                          <option value="tender">按招标文件格式</option>
                        </select>
                      </label>
                      <label>
                        目标篇幅
                        <select value={targetWords} onChange={(event) => setTargetWords(Number(event.target.value))} disabled={!!busy}>
                          <option value={50000}>5 万字</option>
                          <option value={80000}>8 万字</option>
                          <option value={100000}>10 万字</option>
                          <option value={150000}>15 万字</option>
                          <option value={200000}>20 万字</option>
                        </select>
                      </label>
                      <button className="ghost-button" onClick={buildOutline} disabled={!!busy || !canWrite}>
                        {busy === 'outline' ? '生成中…' : '重建大纲'}
                      </button>
                    </div>
                    <div className="gen-actions">
                      <button
                        className="primary-button"
                        onClick={() => startGeneration('missing')}
                        disabled={!!busy || !canWrite || ['queued', 'running'].includes(generationJob?.status || '')}
                      >
                        {busy === 'generation' ? '启动中…' : '生成未完成章节'}
                      </button>
                      <button className="ghost-button" onClick={() => startGeneration('retry')} disabled={!!busy || !canWrite}>
                        重跑失败章节
                      </button>
                      <button className="ghost-button" onClick={() => startGeneration('all')} disabled={!!busy || !canWrite}>
                        全部重跑
                      </button>
                      <button className="ghost-button" onClick={runPostprocess} disabled={!!busy || !canWrite}>
                        {busy === 'postprocess' ? '校验中…' : '全文校验'}
                      </button>
                    </div>
                  </div>

                  {generationJob && (
                    <div className={`generation-progress ${generationJob.status}`}>
                      <div className="gen-progress-head">
                        <strong>{generationJob.message}</strong>
                        <span>{generationJob.progress}%</span>
                      </div>
                      <div className="gen-track">
                        <i style={{ width: `${Math.max(2, generationJob.progress)}%` }} />
                      </div>
                      <div className="gen-progress-foot">
                        <span>
                          成功 {generationJob.result?.generated ?? 0} · 失败 {generationJob.result?.failed ?? 0}
                          {generationJob.result?.quality ? ` · ${generationJob.result.quality.characters} 字` : ''}
                        </span>
                        {['queued', 'running'].includes(generationJob.status) && (
                          <button className="ghost-button danger small" onClick={cancelGeneration}>
                            取消生成
                          </button>
                        )}
                      </div>
                    </div>
                  )}

                  <div className="doc-workspace">
                    <aside className="doc-outline">
                      <div className="doc-outline-head">
                        <div>
                          <span>章节目录</span>
                          <small>{document.sections.length} 节</small>
                        </div>
                        <span className="doc-outline-progress">{writtenCount}/{document.sections.length}</span>
                      </div>
                      <div className="doc-outline-list">
                        {document.sections.map((row) => (
                          <button
                            type="button"
                            key={row.id}
                            className={`doc-outline-item ${row.parentKey ? 'child' : ''} ${row.id === activeSectionId ? 'active' : ''}`}
                            onClick={() => switchSection(row.id)}
                            title={row.heading}
                          >
                            <span className="doc-outline-num">{numbers[row.id] || row.position + 1}</span>
                            <span className="doc-outline-title">{row.heading}</span>
                            {row.status === 'pending' ? (
                              <span className="doc-outline-dot pending" title="待编写" />
                            ) : (
                              <span className={`doc-outline-state ${row.status}`}>
                                {SECTION_STATE[row.status] || row.status}
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                      {(() => {
                        const issues: GenerationIssue[] = qualityReport?.issues || generationJob?.result?.issues || [];
                        if (!issues.length) return null;
                        return (
                          <div className="quality-panel doc-outline-quality">
                            <div className="quality-head">
                              <strong>生成后检查</strong>
                              <span>{issues.length} 项</span>
                            </div>
                            <ul className="quality-list">
                              {issues.slice(0, 12).map((issue, index) => (
                                <li className={`sev-${issue.severity}`} key={`${issue.heading}-${index}`}>
                                  <span className="sev-tag">{SEVERITY_LABEL[issue.severity] || issue.severity}</span>
                                  <div>
                                    <strong>{issue.heading || '全文'}</strong>
                                    <p>{issue.message}</p>
                                  </div>
                                </li>
                              ))}
                            </ul>
                          </div>
                        );
                      })()}
                      <div className="doc-outline-foot">
                        <button className="ghost-button" onClick={addSection} disabled={!canWrite || batchRunning}>
                          + 新增章节
                        </button>
                      </div>
                    </aside>

                    <section className="doc-stage">
                      {activeSection ? (
                        <>
                          <header className="doc-stage-head">
                            <div className="doc-stage-head-main">
                              <div className="doc-stage-title">
                                <span className="doc-stage-number">
                                  {numbers[activeSection.id] || activeSection.position + 1}
                                </span>
                                <strong>{activeSection.heading}</strong>
                                <span className={`doc-state ${activeSection.status}`}>
                                  {SECTION_STATE[activeSection.status] || activeSection.status}
                                </span>
                                {activeDirty && <span className="doc-state dirty">未保存</span>}
                                {generating === activeSection.id && <span className="doc-state running">生成中</span>}
                                <span className="doc-stage-count">
                                  目标 {activeSection.targetWords} 字 · 当前 {activeWords} 字
                                </span>
                              </div>
                              {activeSection.intent && (
                                <p className="doc-stage-intent">写作要点：{activeSection.intent}</p>
                              )}
                            </div>
                            <div className="doc-stage-tools">
                              <button
                                className="ghost-button small"
                                onClick={() => moveSection(activeSection, 'up')}
                                disabled={!canWrite || batchRunning || activeSection.position === 0}
                                title="上移章节"
                                aria-label="上移章节"
                              >
                                ↑
                              </button>
                              <button
                                className="ghost-button small"
                                onClick={() => moveSection(activeSection, 'down')}
                                disabled={
                                  !canWrite ||
                                  batchRunning ||
                                  activeSection.position === document.sections.length - 1
                                }
                                title="下移章节"
                                aria-label="下移章节"
                              >
                                ↓
                              </button>
                              <button
                                className="ghost-button"
                                onClick={() => {
                                  if (activeDirty && !window.confirm('当前章节有未保存的修改，重新生成会覆盖，继续？')) return;
                                  generateSection(activeSection);
                                }}
                                disabled={!!generating || !canWrite || batchRunning}
                              >
                                {generating === activeSection.id ? '生成中…' : '重新生成'}
                              </button>
                              <button
                                className="primary-button small"
                                onClick={() => saveSection(activeSection)}
                                disabled={
                                  saving === activeSection.id ||
                                  !canWrite ||
                                  !activeDirty ||
                                  generating === activeSection.id
                                }
                              >
                                {saving === activeSection.id ? '保存中…' : '保存'}
                              </button>
                              <button
                                className="ghost-button danger small"
                                onClick={() => removeSection(activeSection)}
                                disabled={!canWrite || batchRunning || generating === activeSection.id}
                              >
                                删除
                              </button>
                            </div>
                          </header>
                          <div className="doc-stage-body">
                            {showScore ? (
                              <div className="doc-score">
                                <header className="doc-score-head">
                                  <div>
                                    <p className="eyebrow">SCORE ALIGNMENT · 评分对齐</p>
                                    <h3>目录覆盖 {scoreCoverage.pct}%</h3>
                                  </div>
                                  <span className="doc-state">待编写</span>
                                </header>
                                <p className="doc-score-lead">
                                  正文尚未生成。下面按当前目录核对评分项覆盖；生成正文后这里会切换为正文编辑器。
                                </p>
                                {scoreCoverage.rows.length ? (
                                  <>
                                    <div className="doc-score-summary">
                                      <strong
                                        className={
                                          scoreCoverage.pct >= 80
                                            ? 'ok'
                                            : scoreCoverage.pct >= 60
                                              ? 'warn'
                                              : 'bad'
                                        }
                                      >
                                        {scoreCoverage.pct}%
                                      </strong>
                                      <span>已覆盖 {scoreCoverage.covered} / {scoreCoverage.total} 分</span>
                                      <span>未覆盖 {scoreCoverage.uncovered.length} 项</span>
                                    </div>
                                    {scoreCoverage.uncovered.length > 0 && (
                                      <div className="doc-score-gap">
                                        <strong>尚无章节承载</strong>
                                        <div>
                                          {scoreCoverage.uncovered.map((row) => (
                                            <span key={row.name} className="doc-score-pill">
                                              {row.name} · {row.points} 分
                                            </span>
                                          ))}
                                        </div>
                                      </div>
                                    )}
                                    <table className="doc-score-table">
                                      <thead>
                                        <tr><th>评分项</th><th>分值</th><th>承载章节</th></tr>
                                      </thead>
                                      <tbody>
                                        {scoreCoverage.rows.map((row) => {
                                          const carrier = !!activeSection && row.chapters.some(
                                            (item) => item.id === activeSection.id,
                                          );
                                          return (
                                            <tr
                                              key={row.name}
                                              className={row.chapters.length ? (carrier ? 'active' : '') : 'missing'}
                                            >
                                              <td className="doc-score-name">{row.name}</td>
                                              <td className="doc-score-points">{row.points} 分</td>
                                              <td className="doc-score-chapters">
                                                {row.chapters.length
                                                  ? `${row.chapters.slice(0, 4).map((item) => item.heading).join('、')}${
                                                      row.chapters.length > 4 ? ` 等 ${row.chapters.length} 节` : ''
                                                    }`
                                                  : '无（建议新增对应章节）'}
                                              </td>
                                            </tr>
                                          );
                                        })}
                                      </tbody>
                                    </table>
                                  </>
                                ) : (
                                  <p className="notice">
                                    还没有评分办法数据，先完成招标解读后再生成大纲。
                                  </p>
                                )}
                                <div className="doc-score-meta">
                                  <span>当前章节目标 {activeSection.targetWords} 字</span>
                                  {activeSection.intent && <span>写作要点：{activeSection.intent}</span>}
                                </div>
                                <div className="doc-score-actions">
                                  <button
                                    className="primary-button"
                                    onClick={() => generateSection(activeSection)}
                                    disabled={!!generating || !canWrite || batchRunning}
                                  >
                                    AI 编写本章
                                  </button>
                                  <button className="ghost-button" onClick={() => setManualEdit(true)} disabled={!canWrite}>
                                    手动编写
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <textarea
                                  className="doc-editor"
                                  value={activeBody}
                                  onChange={(event) =>
                                    setDrafts((prev) => ({ ...prev, [activeSection.id]: event.target.value }))
                                  }
                                  placeholder="输入本章正文，支持 Markdown：## 小标题、**加粗**、- 项目符号、| 表格 |、![](图片)"
                                  readOnly={generating === activeSection.id || !canWrite}
                                  spellCheck={false}
                                />
                                <div className="doc-edit-foot">
                                  <span>正文按 Markdown 保存，导出 Word 时会转为正式排版。</span>
                                  <span>
                                    {saving === activeSection.id
                                      ? '保存中…'
                                      : activeDirty
                                        ? '有未保存的修改'
                                        : `约 ${activeWords} 字`}
                                  </span>
                                </div>
                              </>
                            )}
                          </div>
                        </>
                      ) : (
                        <div className="doc-stage-empty">请选择左侧章节目录中的任意章节。</div>
                      )}
                    </section>
                  </div>
                </>
              )}
            </div>
          )}

          {tab === 'review' && (
            <div className="review-panel">
              <div className="panel-head">
                <div>
                  <p className="eyebrow">QUALITY REVIEW · 核对 Agent</p>
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
                  <p className="eyebrow">REVIEW · 核对 / 修复 Agent</p>
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

