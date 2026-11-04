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
    return <LoginPage onLogin={setAccount} />;
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
