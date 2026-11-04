import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, type Account, type Assistant, type Provider } from '../api';

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

export function AiSettingsPage({ account, onLogout }: Props) {
  const navigate = useNavigate();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [assistants, setAssistants] = useState<Assistant[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState<Provider | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_PROVIDER });
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      const [p, a] = await Promise.all([api.providers(), api.assistants()]);
      setProviders(p.items);
      setAssistants(a.items);
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

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="/">
          <span className="brand-mark">智</span>
          <span className="brand-copy"><strong>智标</strong><small>BID INTELLIGENCE</small></span>
        </a>
        <nav className="topnav">
          <a href="/">工作台</a>
          <a className="active" href="/settings/ai">模型服务</a>
        </nav>
        <div className="topbar-right">
          <span className="account-chip">{account.displayName.slice(0, 1)}</span>
          <button className="ghost-button" onClick={onLogout}>退出</button>
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
            <div><h2>协作助手</h2><p>用于区分不同环节的调用配置</p></div>
          </div>
          <div className="assistant-list">
            {assistants.map((assistant) => (
              <div className="assistant-row" key={assistant.id}>
                <div><strong>{assistant.label}</strong><span>{assistant.purpose || assistant.key}</span></div>
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
