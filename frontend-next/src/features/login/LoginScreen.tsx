'use client';

import { useEffect, useState } from 'react';
import { LogOut } from 'lucide-react';
import {
  fetchLoginStatus,
  sendCode,
  loginByAccount,
  logout,
  type LoginStatus,
} from '@/lib/api';
import { showToast, Card } from '../ui';

/**
 * 账号 — 已登录显示昵称/手机号 + 退出；未登录双 Tab（验证码 / 密码）。
 */
export function LoginScreen() {
  const [status, setStatus] = useState<LoginStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<'code' | 'pass'>('pass');
  const [mobile, setMobile] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');

  const load = async () => {
    try {
      setStatus(await fetchLoginStatus());
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const onSend = async () => {
    if (!mobile.trim()) return showToast('请输入手机号', 'info');
    try {
      await sendCode(mobile.trim());
      showToast('验证码已发送', 'success');
    } catch {
      showToast('发送失败', 'error');
    }
  };

  const onLogin = async () => {
    if (!mobile.trim()) return showToast('请输入手机号', 'info');
    try {
      if (tab === 'pass') {
        if (!password) return showToast('请输入密码', 'info');
        await loginByAccount(mobile.trim(), password);
      } else {
        if (!code.trim()) return showToast('请输入验证码', 'info');
        await sendCode(mobile.trim()).catch(() => {});
        await loginByAccount(mobile.trim(), code.trim());
      }
      showToast('登录成功', 'success');
      await load();
    } catch {
      showToast('登录失败', 'error');
    }
  };

  const onLogout = async () => {
    try {
      await logout();
      showToast('已退出', 'success');
      await load();
    } catch {
      showToast('退出失败', 'error');
    }
  };

  if (loading) {
    return (
      <div className="app-hub">
        <div className="app-hub__top">
          <h1 className="app-hub__title">账号</h1>
        </div>
        <div className="app-hub__scroll">
          <div className="baoyu-list">
            <div className="baoyu-row skeleton" />
            <div className="baoyu-row skeleton" />
          </div>
        </div>
      </div>
    );
  }

  const info = status?.account_info;

  return (
    <div className="app-hub">
      <div className="app-hub__top">
        <h1 className="app-hub__title">账号</h1>
      </div>
      <div className="app-hub__scroll">
      {status?.has_account && info ? (
        <Card className="baoyu-account">
          <div className="baoyu-account-avatar">{info.nickname?.[0] || info.mobile?.[0] || 'B'}</div>
          <div className="baoyu-account-body">
            <span className="baoyu-row-name">{info.nickname || '已登录'}</span>
            <span className="baoyu-row-meta">{info.mobile || info.email || ''}</span>
          </div>
          <button type="button" className="baoyu-act danger" onClick={onLogout}>
            <LogOut size={15} />
            <span>退出</span>
          </button>
        </Card>
      ) : (
        <Card className="baoyu-login">
          <div className="baoyu-tabs">
            <button
              type="button"
              className={tab === 'pass' ? 'on' : ''}
              onClick={() => setTab('pass')}
            >
              密码登录
            </button>
            <button
              type="button"
              className={tab === 'code' ? 'on' : ''}
              onClick={() => setTab('code')}
            >
              验证码
            </button>
          </div>
          <input
            className="baoyu-input"
            value={mobile}
            onChange={(e) => setMobile(e.target.value)}
            placeholder="手机号"
            inputMode="tel"
          />
          {tab === 'pass' ? (
            <input
              className="baoyu-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="密码"
            />
          ) : (
            <div className="baoyu-row-acts">
              <input
                className="baoyu-input"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="验证码"
                inputMode="numeric"
              />
              <button type="button" className="baoyu-btn" onClick={onSend}>
                发送
              </button>
            </div>
          )}
          <button type="button" className="baoyu-btn primary" onClick={onLogin}>
            登录
          </button>
        </Card>
      )}
      </div>
    </div>
  );
}
