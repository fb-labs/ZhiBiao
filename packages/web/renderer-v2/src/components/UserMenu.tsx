import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import type { Account } from '../api';

const ROLE_LABEL: Record<string, string> = {
  admin: '系统管理员',
  manager: '投标经理',
  editor: '标书编辑',
  reviewer: '评标审核',
};

type Props = {
  account: Account;
  onLogout: () => void;
};

export function UserMenu({ account, onLogout }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const location = useLocation();

  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && wrapRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const initial = account.displayName.trim().charAt(0) || account.username.charAt(0).toUpperCase();
  const roleLabel = account.isSuperuser
    ? '超级管理员'
    : ROLE_LABEL[account.roles[0]] || '团队成员';
  const canManageAi = account.isSuperuser || account.permissions.includes('admin.ai');
  const canManageAccounts = account.permissions.includes('admin.accounts');

  const go = (path: string) => {
    setOpen(false);
    window.location.href = path;
  };

  return (
    <div className="user-menu" ref={wrapRef}>
      <button
        className="user-button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={account.displayName}
      >
        <span className="user-avatar">{initial}</span>
        <span className="user-meta">
          <strong>{account.displayName}</strong>
          <small>{roleLabel}</small>
        </span>
      </button>

      {open && (
        <div className="user-popover" role="menu">
          <div className="user-popover-head">
            <span className="user-avatar large">{initial}</span>
            <div>
              <strong>{account.displayName}</strong>
              <small>{account.email || `@${account.username}`}</small>
            </div>
          </div>

          {(canManageAi || canManageAccounts) && (
            <div className="user-popover-group">
              {canManageAi && (
                <button className="user-menu-item" onClick={() => go('/settings/ai')} role="menuitem">
                  <span>模型服务</span>
                  <small>API Key、模型与助手</small>
                </button>
              )}
              {canManageAccounts && (
                <button className="user-menu-item" onClick={() => go('/settings/accounts')} role="menuitem">
                  <span>账号权限</span>
                  <small>成员、角色与审计</small>
                </button>
              )}
            </div>
          )}

          <button className="user-menu-item danger" onClick={onLogout} role="menuitem">
            <span>退出登录</span>
            <small>结束当前会话</small>
          </button>
        </div>
      )}
    </div>
  );
}
