import { useEffect, useState } from 'react';
import { api, type Account, type AdminAccount, type RoleInfo } from '../api';

type Props = {
  account: Account;
  onLogout: () => void;
};

type FormState = {
  username: string;
  password: string;
  displayName: string;
  email: string;
  roles: string[];
  isSuperuser: boolean;
  isActive: boolean;
};

const EMPTY: FormState = {
  username: '',
  password: '',
  displayName: '',
  email: '',
  roles: ['editor'],
  isSuperuser: false,
  isActive: true,
};

export function AccountsPage({ account, onLogout }: Props) {
  const [accounts, setAccounts] = useState<AdminAccount[]>([]);
  const [roles, setRoles] = useState<RoleInfo[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<FormState | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [accountList, roleList] = await Promise.all([api.accounts(), api.roles()]);
      setAccounts(accountList.items);
      setRoles(roleList.items);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const openCreate = () => {
    setEditingId(null);
    setForm({ ...EMPTY, roles: ['editor'] });
  };

  const openEdit = (row: AdminAccount) => {
    setEditingId(row.id);
    setForm({
      username: row.username,
      password: '',
      displayName: row.displayName,
      email: row.email || '',
      roles: row.roles,
      isSuperuser: row.isSuperuser,
      isActive: row.isActive,
    });
  };

  const toggleRole = (code: string) => {
    setForm((prev) =>
      prev
        ? {
            ...prev,
            roles: prev.roles.includes(code)
              ? prev.roles.filter((item) => item !== code)
              : [...prev.roles, code],
          }
        : prev,
    );
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!form || busy) return;
    setBusy(true);
    setError('');
    try {
      if (editingId) {
        const payload: Record<string, unknown> = {
          displayName: form.displayName,
          email: form.email || null,
          roles: form.roles,
          isActive: form.isActive,
          isSuperuser: form.isSuperuser,
        };
        if (form.password) payload.password = form.password;
        await api.updateAccount(editingId, payload);
      } else {
        await api.createAccount({
          username: form.username,
          password: form.password,
          displayName: form.displayName || form.username,
          email: form.email || null,
          roles: form.roles,
          isSuperuser: form.isSuperuser,
        });
      }
      setForm(null);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const toggleActive = async (row: AdminAccount) => {
    try {
      await api.updateAccount(row.id, { isActive: !row.isActive });
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '操作失败');
    }
  };

  const remove = async (row: AdminAccount) => {
    if (!window.confirm(`确定删除账号 ${row.username}？`)) return;
    try {
      await api.deleteAccount(row.id);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '删除失败');
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
          <a className="active" href="/settings/accounts">账号权限</a>
          {(account.isSuperuser || account.permissions.includes('admin.ai')) && <a href="/settings/ai">模型服务</a>}
        </nav>
        <div className="topbar-right">
          <span className="account-chip">{account.displayName.slice(0, 1)}</span>
          <button className="ghost-button" onClick={onLogout}>退出</button>
        </div>
      </header>

      <main className="workspace">
        <div className="panel-head">
          <div>
            <p className="eyebrow">ACCESS CONTROL</p>
            <h1>账号与权限</h1>
            <p className="panel-lead">通过角色分配权限：管理员、投标经理、标书编辑、评标审核。</p>
          </div>
          <button className="primary-button" onClick={openCreate}>新增账号</button>
        </div>

        {error && <p className="notice error">{error}</p>}

        {loading ? (
          <p className="notice">正在加载账号…</p>
        ) : (
          <div className="account-table">
            <div className="account-row head">
              <span>账号</span><span>角色</span><span>状态</span><span>操作</span>
            </div>
            {accounts.map((row) => (
              <div className="account-row" key={row.id}>
                <div>
                  <strong>{row.displayName}</strong>
                  <small>@{row.username}{row.email ? ` · ${row.email}` : ''}</small>
                </div>
                <div className="role-chips">
                  {row.isSuperuser && <span className="role-chip super">超级管理员</span>}
                  {row.roles.map((code) => (
                    <span className="role-chip" key={code}>
                      {roles.find((role) => role.code === code)?.name || code}
                    </span>
                  ))}
                </div>
                <span className={`status ${row.isActive ? 'status-complete' : 'status-archived'}`}>
                  {row.isActive ? '启用' : '停用'}
                </span>
                <div className="account-actions">
                  <button className="ghost-button" onClick={() => openEdit(row)}>编辑</button>
                  <button className="ghost-button" onClick={() => toggleActive(row)}>
                    {row.isActive ? '停用' : '启用'}
                  </button>
                  <button className="ghost-button danger" onClick={() => remove(row)}>删除</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>

      {form && (
        <div className="modal-mask" onMouseDown={() => setForm(null)}>
          <form className="modal account-modal" onSubmit={submit} onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <div>
                <p className="eyebrow">{editingId ? 'EDIT ACCOUNT' : 'NEW ACCOUNT'}</p>
                <h2>{editingId ? '编辑账号' : '新增账号'}</h2>
              </div>
              <button type="button" className="icon-button" onClick={() => setForm(null)}>×</button>
            </div>

            <label className="field">
              <span>账号</span>
              <input
                value={form.username}
                onChange={(event) => setForm({ ...form, username: event.target.value })}
                disabled={!!editingId}
                placeholder="登录账号"
                required
              />
            </label>
            <label className="field">
              <span>显示名称</span>
              <input
                value={form.displayName}
                onChange={(event) => setForm({ ...form, displayName: event.target.value })}
                placeholder="例如：投标经理"
              />
            </label>
            <label className="field">
              <span>邮箱（可选）</span>
              <input
                value={form.email}
                onChange={(event) => setForm({ ...form, email: event.target.value })}
                placeholder="用于登录或通知"
              />
            </label>
            <label className="field">
              <span>{editingId ? '重置密码（留空则不修改）' : '初始密码'}</span>
              <input
                type="password"
                value={form.password}
                onChange={(event) => setForm({ ...form, password: event.target.value })}
                placeholder="至少 6 位"
                required={!editingId}
              />
            </label>

            <div className="field">
              <span>角色</span>
              <div className="role-options">
                {roles.map((role) => (
                  <label key={role.code} className="role-option">
                    <input
                      type="checkbox"
                      checked={form.roles.includes(role.code)}
                      onChange={() => toggleRole(role.code)}
                    />
                    <span>
                      <strong>{role.name}</strong>
                      <small>{role.description}</small>
                    </span>
                  </label>
                ))}
              </div>
            </div>

            <label className="checkbox-line">
              <input
                type="checkbox"
                checked={form.isSuperuser}
                onChange={(event) => setForm({ ...form, isSuperuser: event.target.checked })}
              />
              <span>设为超级管理员（拥有全部权限）</span>
            </label>

            {editingId && (
              <label className="checkbox-line">
                <input
                  type="checkbox"
                  checked={form.isActive}
                  onChange={(event) => setForm({ ...form, isActive: event.target.checked })}
                />
                <span>账号启用</span>
              </label>
            )}

            <button className="primary-button" type="submit" disabled={busy}>
              {busy ? '保存中…' : '保存账号'}
            </button>
          </form>
        </div>
      )}
    </div>
  );
}
