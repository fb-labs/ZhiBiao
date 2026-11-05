import { useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { api, type Account } from './api';
import { LoginPage } from './pages/Login';
import { HomePage } from './pages/Home';
import { TenderDetailPage } from './pages/TenderDetail';
import { AiSettingsPage } from './pages/AiSettings';
import { AccountsPage } from './pages/Accounts';

export function App() {
  const [account, setAccount] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .me()
      .then(setAccount)
      .catch(() => setAccount(null))
      .finally(() => setLoading(false));
  }, []);

  // 登录后固定回到工作台：即使用户是在别的页面被跳到登录页，也统一落到工作台。
  const signedIn = (next: Account) => {
    if (window.location.pathname !== '/') {
      window.history.replaceState({}, '', '/');
    }
    setAccount(next);
  };

  const logout = async () => {
    try {
      await api.logout();
    } finally {
      setAccount(null);
      window.location.href = '/';
    }
  };

  if (loading) {
    return <div className="boot">正在进入工作台…</div>;
  }

  if (!account) {
    return <LoginPage onLogin={signedIn} />;
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage account={account} onLogout={logout} />} />
        <Route path="/tender/:id" element={<TenderDetailPage account={account} onLogout={logout} />} />
        <Route
          path="/settings/ai"
          element={
            account.isSuperuser || account.permissions.includes('admin.ai') ? (
              <AiSettingsPage account={account} onLogout={logout} />
            ) : (
              <Navigate to="/" replace />
            )
          }
        />
        <Route
          path="/settings/accounts"
          element={
            account.permissions.includes('admin.accounts') ? (
              <AccountsPage account={account} onLogout={logout} />
            ) : (
              <Navigate to="/" replace />
            )
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
