import { useEffect, useState } from 'react';
import { Backdrop } from '../components/Backdrop';
import { api, type Account } from '../api';

const TYPING_HEAD = '变成一份';
const TYPING_ACCENT = '可交付的标书';

/** 刷新页面时逐字打一遍，打完即停；开启「减少动态效果」时直接显示全文。 */
function useTypewriter(total: number, speed = 80) {
  const [typed, setTyped] = useState(0);
  useEffect(() => {
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setTyped(total);
      return;
    }
    let index = 0;
    const timer = window.setInterval(() => {
      index += 1;
      setTyped(index);
      if (index >= total) window.clearInterval(timer);
    }, speed);
    return () => window.clearInterval(timer);
  }, [total, speed]);
  return typed;
}

type Props = {
  onLogin: (account: Account) => void;
};

export function LoginPage({ onLogin }: Props) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const typed = useTypewriter(TYPING_HEAD.length + TYPING_ACCENT.length);
  const headShown = TYPING_HEAD.slice(0, Math.min(typed, TYPING_HEAD.length));
  const accentShown =
    typed > TYPING_HEAD.length
      ? TYPING_ACCENT.slice(0, typed - TYPING_HEAD.length)
      : '';
  const typing = typed < TYPING_HEAD.length + TYPING_ACCENT.length;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.login(username.trim(), password);
      onLogin(result.account);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : '登录失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <Backdrop dense />
      <div className="login-glow" />
      <header className="login-nav">
        <div className="brand">
          <span className="brand-mark"><img src="/logo-tile.png" alt="智标" /></span>
          <span className="brand-copy">
            <strong>智标</strong>
            <small>BID INTELLIGENCE</small>
          </span>
        </div>
        <span className="login-status"><i /> 私有部署 · 数据不出内网</span>
      </header>

      <section className="login-stage">
        <div className="login-copy">
          <p className="eyebrow">投标协作 · 从读到交</p>
          <h1>
            把一份招标文件
            <br />
            <span className="typing-line">
              {headShown}
              <span>{accentShown}</span>
              {typing && <i className="typing-caret" aria-hidden="true" />}
            </span>
          </h1>
          <p className="lead">
            解读、编写、检查、交付在同一条工作线上完成。每个环节都留痕，每一处修改都有依据。
          </p>
          <div className="login-metrics">
            <div><strong>01</strong><span>招标解读</span></div>
            <div><strong>02</strong><span>标书编写</span></div>
            <div><strong>03</strong><span>质量检查</span></div>
            <div><strong>04</strong><span>文档交付</span></div>
          </div>
        </div>

        <form className="login-card" onSubmit={submit}>
          <div className="login-card-head">
            <span className="pill">团队工作台</span>
            <h2>登录智标</h2>
            <p>使用管理员分配的账号进入投标工作台</p>
          </div>
          <label className="field">
            <span>账号</span>
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="请输入账号"
              autoComplete="username"
              autoFocus
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
              autoComplete="current-password"
            />
          </label>
          {error && <p className="form-error">{error}</p>}
          <button className="primary-button" type="submit" disabled={busy}>
            {busy ? '正在验证…' : '进入工作台'}
          </button>
          <p className="login-hint">账号由系统管理员创建，忘记密码请联系管理员重置。</p>
        </form>
      </section>

      <footer className="login-foot">
        <span>智标 · 投标协作平台</span>
        <span>AGPL-3.0 · 私有化部署</span>
      </footer>
    </main>
  );
}
