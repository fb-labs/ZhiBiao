import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AmbientScene } from '../components/AmbientScene';
import { UserMenu } from '../components/UserMenu';
import { api, type Account, type AgentSpec, type Assistant, type Provider } from '../api';

type Props = {
  account: Account;
  onLogout: () => void;
};

const EMPTY_PROVIDER = {
  label: '',
  baseUrl: 'https://token.sensenova.cn/v1',
  apiKey: '',
  model: 'glm-5.2',
  temperature: 0.6,
  maxTokens: 4000,
  isActive: true,
  isDefault: true,
};

type CapabilityDraft = {
  enabled: boolean;
  baseUrl: string;
  apiKey: string;
  apiKeySet: boolean;
  ready: boolean;
  model: string;
  maxPages: number;
  size: string;
  maxPerSection: number;
  maxPerBook: number;
};

const EMPTY_CAPABILITY: CapabilityDraft = {
  enabled: false,
  baseUrl: '',
  apiKey: '',
  apiKeySet: false,
  ready: false,
  model: '',
  maxPages: 10,
  size: '1024x1024',
  maxPerSection: 2,
  maxPerBook: 8,
};

export function AiSettingsPage({ account, onLogout }: Props) {
  const navigate = useNavigate();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [agents, setAgents] = useState<AgentSpec[]>([]);
  const [ocrCap, setOcrCap] = useState<CapabilityDraft>({ ...EMPTY_CAPABILITY });
  const [imageCap, setImageCap] = useState<CapabilityDraft>({ ...EMPTY_CAPABILITY });
  const [capBusy, setCapBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<Provider | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_PROVIDER });
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const [p, a, g, c] = await Promise.all([
        api.providers(),
        api.assistants(),
        api.agents().catch(() => ({ items: [] as AgentSpec[] })),
        api.capabilities().catch(() => null),
      ]);
      setProviders(p.items);
      setAssistants(a.items);
      setAgents(g.items);
      if (c) {
        setOcrCap({ ...EMPTY_CAPABILITY, ...c.ocr, apiKey: '' });
        setImageCap({ ...EMPTY_CAPABILITY, ...c.image, apiKey: '' });
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '加载失败');
    }
  };

  useEffect(() => {
    load();
  }, []);

  const openCreate = () => {
    setEditing(null);
    setForm({ ...EMPTY_PROVIDER });
    setFormOpen(true);
    setError('');
    setNotice('');
  };

  const openEdit = (provider: Provider) => {
    setEditing(provider);
    setForm({
      label: provider.label,
      baseUrl: provider.baseUrl,
      apiKey: '',
      model: provider.model,
      temperature: provider.temperature,
      maxTokens: provider.maxTokens,
      isActive: provider.isActive,
      isDefault: provider.isDefault,
    });
    setFormOpen(true);
    setError('');
    setNotice('');
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      if (editing) {
        await api.updateProvider(editing.id, form);
        setNotice('模型服务已更新');
      } else {
        await api.createProvider(form);
        setNotice('模型服务已添加');
      }
      setEditing(null);
      setFormOpen(false);
      setForm({ ...EMPTY_PROVIDER });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const probe = async (provider: Provider) => {
    setError('');
    setNotice(`正在测试 ${provider.label}…`);
    try {
      const result = await api.probeProvider(provider.id);
      if (result.ok) {
        setNotice(`${provider.label} 可用，${result.latencyMs}ms，返回：${result.reply || 'OK'}`);
      } else {
        setError(`${provider.label} 不可用：${result.error}`);
        setNotice('');
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '测试失败');
      setNotice('');
    }
  };

  const remove = async (provider: Provider) => {
    if (!window.confirm(`删除模型服务「${provider.label}」？`)) return;
    try {
      await api.deleteProvider(provider.id);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '删除失败');
    }
  };

  const toggleAssistant = async (assistant: Assistant) => {
    try {
      await api.updateAssistant(assistant.id, { ...assistant, enabled: !assistant.enabled });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '更新失败');
    }
  };

  const saveCapabilities = async () => {
    if (capBusy) return;
    setCapBusy(true);
    setError('');
    setNotice('');
    try {
      const saved = await api.saveCapabilities({
        ocr: {
          enabled: ocrCap.enabled,
          baseUrl: ocrCap.baseUrl.trim(),
          apiKey: ocrCap.apiKey,
          model: ocrCap.model.trim(),
          maxPages: Math.max(1, Math.min(50, Number(ocrCap.maxPages) || 10)),
        },
        image: {
          enabled: imageCap.enabled,
          baseUrl: imageCap.baseUrl.trim(),
          apiKey: imageCap.apiKey,
          model: imageCap.model.trim(),
          size: imageCap.size.trim() || '1024x1024',
          maxPerSection: Math.max(0, Math.min(8, Number(imageCap.maxPerSection) || 0)),
          maxPerBook: Math.max(0, Math.min(60, Number(imageCap.maxPerBook) || 0)),
        },
      });
      setOcrCap({ ...EMPTY_CAPABILITY, ...saved.ocr, apiKey: '' });
      setImageCap({ ...EMPTY_CAPABILITY, ...saved.image, apiKey: '' });
      setNotice('能力开关已保存');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '保存失败');
    } finally {
      setCapBusy(false);
    }
  };

  return (
    <div className="shell">
      <AmbientScene variant="model" />
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brand-mark"><img src="/logo-tile.png" alt="智标" /></span>
          <span className="brand-copy"><strong>智标</strong><small>BID INTELLIGENCE</small></span>
        </a>
        <nav className="topnav">
          <a href="/">工作台</a>
        </nav>
        <div className="topbar-right">
          <UserMenu account={account} onLogout={onLogout} />
        </div>
      </header>

      <main className="workspace">
        <section className="page-head">
          <p className="eyebrow">PLATFORM</p>
          <h1>模型服务</h1>
          <p>配置兼容 OpenAI 协议的模型服务。默认服务会用于招标解读、标书编写与质量检查。</p>
        </section>

        {error && <p className="notice error">{error}</p>}
        {notice && <p className="notice">{notice}</p>}

        <section className="admin-block">
          <div className="section-head">
            <div><h2>服务列表</h2><p>Key 仅显示首尾四位</p></div>
            <button className="primary-button" onClick={openCreate}>添加服务</button>
          </div>
          <div className="provider-list">
            {providers.map((provider) => (
              <div className="provider-card" key={provider.id}>
                <div className="provider-main">
                  <div>
                    <strong>{provider.label}</strong>
                    <span>{provider.baseUrl}</span>
                  </div>
                  <div className="provider-tags">
                    {provider.isDefault && <span className="tag primary">默认</span>}
                    <span className={`tag ${provider.isActive ? 'ok' : ''}`}>{provider.isActive ? '启用' : '停用'}</span>
                  </div>
                </div>
                <dl>
                  <div><dt>模型</dt><dd>{provider.model || '—'}</dd></div>
                  <div><dt>温度</dt><dd>{provider.temperature}</dd></div>
                  <div><dt>最大 tokens</dt><dd>{provider.maxTokens}</dd></div>
                  <div><dt>API Key</dt><dd>{provider.apiKey || '未配置'}</dd></div>
                </dl>
                <div className="provider-actions">
                  <button className="ghost-button" onClick={() => probe(provider)}>测试</button>
                  <button className="ghost-button" onClick={() => openEdit(provider)}>编辑</button>
                  <button className="ghost-button danger" onClick={() => remove(provider)}>删除</button>
                </div>
              </div>
            ))}
            {providers.length === 0 && <p className="notice">尚未配置模型服务，先添加一个。</p>}
          </div>
        </section>

        <section className="admin-block">
          <div className="section-head">
            <div><h2>能力开关</h2><p>默认关闭；开启并保存后才会调用对应接口</p></div>
            <button className="primary-button" onClick={saveCapabilities} disabled={capBusy}>
              {capBusy ? '保存中…' : '保存能力设置'}
            </button>
          </div>
          <div className="capability-grid">
            <div className="capability-card">
              <div className="capability-head">
                <div>
                  <strong>OCR 接口</strong>
                  <p className="capability-desc">
                    图片上传时自动识别文字，扫描版 PDF 由招标解析按需识别；关闭时保持原有解析逻辑。
                  </p>
                </div>
                <button
                  className={`toggle ${ocrCap.enabled ? 'on' : ''}`}
                  onClick={() => setOcrCap({ ...ocrCap, enabled: !ocrCap.enabled })}
                >
                  {ocrCap.enabled ? '已开启' : '默认关闭'}
                </button>
              </div>
              <label className="field">
                <span>接口地址</span>
                <input
                  value={ocrCap.baseUrl}
                  onChange={(event) => setOcrCap({ ...ocrCap, baseUrl: event.target.value })}
                  placeholder="https://…/v1"
                />
              </label>
              <div className="field-row">
                <label className="field">
                  <span>模型</span>
                  <input
                    value={ocrCap.model}
                    onChange={(event) => setOcrCap({ ...ocrCap, model: event.target.value })}
                    placeholder="glm-4.5v"
                  />
                </label>
                <label className="field">
                  <span>API Key {ocrCap.apiKeySet ? '（已配置，留空保持不变）' : ''}</span>
                  <input
                    type="password"
                    value={ocrCap.apiKey}
                    onChange={(event) => setOcrCap({ ...ocrCap, apiKey: event.target.value })}
                    placeholder="sk-…"
                  />
                </label>
              </div>
              <label className="field">
                <span>最多识别页数（PDF）</span>
                <input
                  type="number"
                  min="1"
                  max="50"
                  value={ocrCap.maxPages}
                  onChange={(event) =>
                    setOcrCap({ ...ocrCap, maxPages: Number(event.target.value) })
                  }
                />
              </label>
              <p className="capability-hint">需兼容 OpenAI 协议的 /chat/completions 视觉接口。</p>
            </div>

            <div className="capability-card">
              <div className="capability-head">
                <div>
                  <strong>标书生图</strong>
                  <p className="capability-desc">
                    开启后只为示意图、流程图、架构图类占位框生成插图；证照、合同、截图等扫描件仍保留占位。
                  </p>
                </div>
                <button
                  className={`toggle ${imageCap.enabled ? 'on' : ''}`}
                  onClick={() => setImageCap({ ...imageCap, enabled: !imageCap.enabled })}
                >
                  {imageCap.enabled ? '已开启' : '默认关闭'}
                </button>
              </div>
              <label className="field">
                <span>接口地址</span>
                <input
                  value={imageCap.baseUrl}
                  onChange={(event) => setImageCap({ ...imageCap, baseUrl: event.target.value })}
                  placeholder="https://…/v1"
                />
              </label>
              <div className="field-row">
                <label className="field">
                  <span>模型</span>
                  <input
                    value={imageCap.model}
                    onChange={(event) => setImageCap({ ...imageCap, model: event.target.value })}
                    placeholder="图像生成模型"
                  />
                </label>
                <label className="field">
                  <span>API Key {imageCap.apiKeySet ? '（已配置，留空保持不变）' : ''}</span>
                  <input
                    type="password"
                    value={imageCap.apiKey}
                    onChange={(event) => setImageCap({ ...imageCap, apiKey: event.target.value })}
                    placeholder="sk-…"
                  />
                </label>
              </div>
              <div className="field-row">
                <label className="field">
                  <span>图片尺寸</span>
                  <input
                    value={imageCap.size}
                    onChange={(event) => setImageCap({ ...imageCap, size: event.target.value })}
                    placeholder="1024x1024"
                  />
                </label>
                <label className="field">
                  <span>每章最多生成</span>
                  <input
                    type="number"
                    min="0"
                    max="8"
                    value={imageCap.maxPerSection}
                    onChange={(event) =>
                      setImageCap({ ...imageCap, maxPerSection: Number(event.target.value) })
                    }
                  />
                </label>
              </div>
              <label className="field">
                <span>每本最多生成</span>
                <input
                  type="number"
                  min="0"
                  max="60"
                  value={imageCap.maxPerBook}
                  onChange={(event) =>
                    setImageCap({ ...imageCap, maxPerBook: Number(event.target.value) })
                  }
                />
              </label>
              <p className="capability-hint">
                需兼容 OpenAI 协议的 /images/generations 接口；生成结果随标书导出嵌入 DOCX。
              </p>
            </div>
          </div>
        </section>

        <section className="admin-block">
          <div className="section-head">
            <div><h2>协作助手</h2><p>用于区分不同环节的调用配置</p></div>
          </div>
          <div className="assistant-list">
            {assistants.map((assistant) => (
              <div className="assistant-row" key={assistant.id}>
                <div>
                  <strong>{assistant.label}</strong>
                  <span>{assistant.purpose || assistant.key}</span>
                  <span>
                    {(() => {
                      const linked = agents.filter((agent) => agent.assistant === assistant.key).map((agent) => agent.name);
                      return linked.length ? `关联：${linked.join('、')}` : '未关联 Agent';
                    })()}
                  </span>
                </div>
                <span>{assistant.model || '继承默认'}</span>
                <span>{assistant.temperature}</span>
                <button className={`toggle ${assistant.enabled ? 'on' : ''}`} onClick={() => toggleAssistant(assistant)}>
                  {assistant.enabled ? '启用' : '停用'}
                </button>
              </div>
            ))}
            {assistants.length === 0 && <p className="notice">尚未创建协作助手。可在后续阶段配置解读、编写、检查与交付助手。</p>}
          </div>
        </section>

        <button className="ghost-button" onClick={() => navigate('/')}>返回工作台</button>
      </main>

      {formOpen && (
        <div className="modal-mask" onMouseDown={() => { setEditing(null); setFormOpen(false); setForm({ ...EMPTY_PROVIDER }); }}>
          <form className="modal" onMouseDown={(event) => event.stopPropagation()} onSubmit={save}>
            <div className="modal-head">
              <div><p className="eyebrow">{editing ? 'EDIT PROVIDER' : 'NEW PROVIDER'}</p><h2>{editing ? '编辑模型服务' : '添加模型服务'}</h2></div>
              <button type="button" className="icon-button" onClick={() => { setEditing(null); setFormOpen(false); setForm({ ...EMPTY_PROVIDER }); }}>×</button>
            </div>
            <label className="field">
              <span>名称 *</span>
              <input value={form.label} onChange={(event) => setForm({ ...form, label: event.target.value })} placeholder="例如：商汤 Token 服务" autoFocus />
            </label>
            <label className="field">
              <span>API Base *</span>
              <input value={form.baseUrl} onChange={(event) => setForm({ ...form, baseUrl: event.target.value })} placeholder="https://…/v1" />
            </label>
            <div className="field-row">
              <label className="field">
                <span>模型</span>
                <input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="glm-5.2" />
              </label>
              <label className="field">
                <span>API Key {editing ? '（留空保持不变）' : ''}</span>
                <input type="password" value={form.apiKey} onChange={(event) => setForm({ ...form, apiKey: event.target.value })} placeholder="sk-…" />
              </label>
            </div>
            <div className="field-row">
              <label className="field">
                <span>温度</span>
                <input type="number" step="0.1" min="0" max="2" value={form.temperature} onChange={(event) => setForm({ ...form, temperature: Number(event.target.value) })} />
              </label>
              <label className="field">
                <span>最大 tokens</span>
                <input type="number" min="64" max="32000" value={form.maxTokens} onChange={(event) => setForm({ ...form, maxTokens: Number(event.target.value) })} />
              </label>
            </div>
            <div className="check-row">
              <label><input type="checkbox" checked={form.isActive} onChange={(event) => setForm({ ...form, isActive: event.target.checked })} /> 启用</label>
              <label><input type="checkbox" checked={form.isDefault} onChange={(event) => setForm({ ...form, isDefault: event.target.checked })} /> 设为默认</label>
            </div>
            {error && <p className="form-error">{error}</p>}
            <div className="modal-actions">
              <button type="button" className="ghost-button" onClick={() => { setEditing(null); setFormOpen(false); setForm({ ...EMPTY_PROVIDER }); }}>取消</button>
              <button type="submit" className="primary-button" disabled={busy}>{busy ? '保存中…' : '保存'}</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
