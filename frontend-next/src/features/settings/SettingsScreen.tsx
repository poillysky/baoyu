'use client';

import { useEffect, useState, useCallback } from 'react';
import {
  User,
  LogOut,
  Network,
  BadgeCheck,
  RefreshCw,
  ChevronRight,
  Check,
  X,
  Activity,
  Cpu,
  Film,
  Trash2,
  Globe,
  Smartphone,
  Database,
  Video,
  Zap,
  MonitorOff,
} from 'lucide-react';
import {
  fetchLoginStatus,
  sendCode,
  loginByAccount,
  logout,
  fetchSettingsAll,
  setProxy,
  setMode,
  runNetTest,
  fetchCacheStats,
  clearCache,
  type LoginStatus,
  type NetTestResult,
  type SettingsAll,
  type CacheStats,
} from '@/lib/api';
import { showToast, Card } from '../ui';
import { AppPush } from '@/components/ui/AppPush';
import { useTabNavigation } from '@/shell/TabContext';

/**
 * 设置页 — 入口列表 → 二级页。
 *   入口：1.账号管理 2.网络管理 3.服务端网络测试 4.运行模式 5.录制模式
 *   点击进入对应二级页（AppPush，带返回）。
 */
type SectionKey = 'account' | 'network' | 'nettest' | 'record' | 'cache';

export function SettingsScreen() {
  const [section, setSection] = useState<SectionKey | null>(null);

  // 切到「设置」tab 时回到入口列表
  const tabCtx = useTabNavigation();
  useEffect(() => {
    if (tabCtx?.activeTab === '/settings') {
      setSection(null);
    }
  }, [tabCtx?.activeTab]);

  if (section) {
    return (
      <SettingsSubSection
        section={section}
        onBack={() => setSection(null)}
      />
    );
  }

  return (
    <div className="app-hub">
      <div className="app-hub__top">
        <h1 className="app-hub__title">设置</h1>
      </div>
      <div className="app-hub__scroll baoyu-settings">
        <div className="baoyu-settings-group">
          <EntryRow icon={<User size={18} />} title="账号管理" desc="账号 · 身份" onClick={() => setSection('account')} />
          <EntryRow icon={<Network size={18} />} title="网络管理" desc="代理设置" onClick={() => setSection('network')} />
          <EntryRow icon={<Activity size={18} />} title="网络测试" desc="直播链路检测" onClick={() => setSection('nettest')} />
          <EntryRow icon={<Film size={18} />} title="录制模式" desc="Web · 原生" onClick={() => setSection('record')} />
          <EntryRow icon={<Trash2 size={18} />} title="缓存管理" desc="图片缓存清理" onClick={() => setSection('cache')} />
        </div>
      </div>
    </div>
  );
}

function EntryRow({
  icon,
  title,
  desc,
  onClick,
}: {
  icon: React.ReactNode;
  title: string;
  desc: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className="baoyu-row baoyu-entry" onClick={onClick}>
      <span className="baoyu-entry-icon">{icon}</span>
      <span className="baoyu-row-main">
        <span className="baoyu-row-name">{title}</span>
        <span className="baoyu-row-meta">{desc}</span>
      </span>
      <ChevronRight size={18} className="baoyu-row-chev" />
    </button>
  );
}

/* ────────────────────────── 二级页容器 ────────────────────────── */

function SettingsSubSection({
  section,
  onBack,
}: {
  section: SectionKey;
  onBack: () => void;
}) {
  const title =
    section === 'account'
      ? '账号管理'
      : section === 'network'
        ? '网络管理'
        : section === 'nettest'
          ? '网络测试'
          : section === 'record'
            ? '录制模式'
            : '缓存管理';

  return (
    <AppPush title={title} onBack={onBack} scrollKey={`settings-${section}`} scrollMode="top">
      <div className="baoyu-settings baoyu-settings-sub">
        {section === 'account' && <AccountSection />}
        {section === 'network' && <NetworkSection />}
        {section === 'nettest' && <NetTestSection />}
        {section === 'record' && <RecordSection />}
        {section === 'cache' && <CacheSection />}
      </div>
    </AppPush>
  );
}

/* ────────────────────────── 1. 账号管理 ────────────────────────── */

function AccountSection() {
  const [status, setStatus] = useState<LoginStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<'code' | 'pass'>('pass');
  const [mobile, setMobile] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [isMember, setIsMember] = useState(false);

  const load = useCallback(async () => {
    try {
      const st = await fetchLoginStatus();
      setStatus(st);
      // 会员身份以「已登录且账号激活」为准（account_active = 会员 token 生效）
      setIsMember(!!st?.has_account && !!st?.account_active);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

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

  const info = status?.account_info;

  return (
    <>
      <SectionTitle icon={<User size={15} />} text="登录信息" />
      {loading ? (
        <Card className="baoyu-account">
          <div className="baoyu-row skeleton" />
          <div className="baoyu-row skeleton" />
        </Card>
      ) : status?.has_account && info ? (
        <Card className="baoyu-account">
          <div className="baoyu-account-avatar">
            {info.nickname?.[0] || info.mobile?.[0] || 'B'}
          </div>
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

      <SectionTitle icon={<BadgeCheck size={15} />} text="当前身份" />
      <Card className="baoyu-about">
        <NetRow
          icon={isMember ? <BadgeCheck size={16} /> : <User size={16} />}
          label="当前身份"
          value={isMember ? '会员 · 不限次数' : '游客 · 按 IP 计次数'}
          ok={isMember}
        />
        <p className="baoyu-net-hint">
          会员需在服务端激活账号；游客按 IP 每日限制次数。
        </p>
      </Card>
    </>
  );
}

/* ────────────────────────── 2. 网络管理 ────────────────────────── */

function NetworkSection() {
  const [proxyEnabled, setProxyEnabled] = useState(false);
  const [proxyUrl, setProxyUrl] = useState('');
  const [proxySaving, setProxySaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const s = await fetchSettingsAll();
      setProxyEnabled(!!s.proxy?.proxy_enabled);
      setProxyUrl(s.proxy?.proxy_url || '');
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onToggleProxy = async (next: boolean) => {
    setProxySaving(true);
    try {
      const r = await setProxy(next, proxyUrl || undefined);
      setProxyEnabled(!!r?.proxy_enabled);
      setProxyUrl(r?.proxy_url || proxyUrl || '');
      showToast(next ? '已开启代理' : '已关闭代理', 'success');
    } catch {
      showToast('代理设置失败', 'error');
    } finally {
      setProxySaving(false);
    }
  };

  const onSaveProxyUrl = async () => {
    setProxySaving(true);
    try {
      const r = await setProxy(proxyEnabled, proxyUrl || undefined);
      setProxyUrl(r?.proxy_url || '');
      showToast('代理地址已保存', 'success');
    } catch {
      showToast('保存失败', 'error');
    } finally {
      setProxySaving(false);
    }
  };

  return (
    <>
      <SectionTitle icon={<Network size={15} />} text="代理配置" />
      <Card className="baoyu-network">
        <div className="baoyu-switch-row">
          <span className="baoyu-net-label">走代理</span>
          <span className="baoyu-net-value">
            {proxyEnabled ? '已开启' : '已关闭'}
          </span>
          <button
            type="button"
            className={`baoyu-switch ${proxyEnabled ? 'on' : ''}`}
            onClick={() => onToggleProxy(!proxyEnabled)}
            disabled={proxySaving}
            aria-label="切换代理"
          >
            <span className="baoyu-switch-knob" />
          </button>
        </div>
        <div className="baoyu-switch-row">
          <input
            className="baoyu-input"
            value={proxyUrl}
            onChange={(e) => setProxyUrl(e.target.value)}
            placeholder="http://127.0.0.1:7897"
            inputMode="url"
            disabled={!proxyEnabled}
          />
          <button
            type="button"
            className="baoyu-btn"
            onClick={onSaveProxyUrl}
            disabled={proxySaving || !proxyEnabled}
          >
            保存
          </button>
        </div>
        <p className="baoyu-net-hint">
          代理仅影响封面/业务 HTTP 抓取；签权隧道、拉流为公网直连，不经代理。
        </p>
      </Card>
    </>
  );
}

/* ────────────────────────── 3. 服务端网络测试 ────────────────────────── */

const DEFAULT_PROBES = [
  { name: '签权隧道 (UDP/KCP)', target: '162.14.199.148:9875' },
  { name: 'bootstrap 节点池 (UDP)', target: '43.199.86.204:8003' },
  { name: '业务 API (TCP)', target: '103.44.236.131:8001' },
  { name: '业务接口 (AES)', target: '103.44.236.131:8001/platforms' },
  { name: 'Agora 媒体边缘', target: 'ap-web-1-asia / ap-proxy-1' },
  { name: '封面图床 IPv6', target: 'yn7ux.ooat88.com' },
  { name: '封面图床 IPv4', target: 'yn7ux.ooat88.com' },
  { name: 'DNS 双栈记录', target: 'yn7ux.ooat88.com' },
];

function NetTestSection() {
  const [netTest, setNetTest] = useState<NetTestResult | null>(null);
  const [netTestLoading, setNetTestLoading] = useState(false);

  const onRunNetTest = async () => {
    setNetTestLoading(true);
    setNetTest(null);
    try {
      setNetTest(await runNetTest());
    } catch {
      showToast('网络测试失败', 'error');
    } finally {
      setNetTestLoading(false);
    }
  };

  return (
    <>
      <SectionTitle icon={<Activity size={15} />} text="链路探测" />
      <Card className="baoyu-network">
        <div className="baoyu-network-refresh" style={{ justifyContent: 'flex-start' }}>
          <button
            type="button"
            className="baoyu-btn primary"
            onClick={onRunNetTest}
            disabled={netTestLoading}
          >
            <RefreshCw size={14} className={netTestLoading ? 'spin' : ''} />
            <span>{netTestLoading ? '测试中…' : '开始测试'}</span>
          </button>
        </div>
        <div className="baoyu-nettest">
          {DEFAULT_PROBES.map((p, i) => {
            const r = netTest?.results[i];
            return (
              <div className="baoyu-nettest-row" key={p.name}>
                <span className={`baoyu-nettest-ic ${r ? (r.ok ? 'ok' : 'bad') : 'idle'}`}>
                  {r ? (
                    r.ok ? <Check size={14} /> : <X size={14} />
                  ) : (
                    <span className="baoyu-nettest-dot" />
                  )}
                </span>
                <div className="baoyu-nettest-body">
                  <span className="baoyu-nettest-name">{p.name}</span>
                  <span className="baoyu-nettest-target">{p.target}</span>
                </div>
                <div className={`baoyu-nettest-res ${r ? (r.ok ? '' : 'off') : 'idle'}`}>
                  {!r ? '—' : r.ok ? `${r.ms}ms` : (r.msg || '失败')}
                </div>
              </div>
            );
          })}
          {netTest && (
            <div className={`baoyu-nettest-all ${netTest.all_ok ? 'ok' : 'bad'}`}>
              {netTest.all_ok ? '全部链路可达' : '存在不可达项'}
            </div>
          )}
        </div>
      </Card>
    </>
  );
}

/* ────────────────────────── 6. 缓存管理 ────────────────────────── */

function CacheSection() {
  const [cache, setCache] = useState<CacheStats | null>(null);
  const [cacheBusy, setCacheBusy] = useState(false);

  const refreshCache = async () => {
    try {
      setCache(await fetchCacheStats());
    } catch {
      /* 静默 */
    }
  };
  useEffect(() => {
    refreshCache();
  }, []);

  const onClearCache = async () => {
    setCacheBusy(true);
    try {
      const r = await clearCache();
      setCache(r);
      showToast(`已清除 ${r.cleared ?? 0} 张图片缓存`, 'success');
    } catch {
      showToast('清缓存失败', 'error');
    } finally {
      setCacheBusy(false);
    }
  };

  const fmtSize = (b?: number) => {
    if (b == null)
      return '';
    if (b < 1024)
      return `${b}B`;
    if (b < 1048576)
      return `${(b / 1024).toFixed(1)}KB`;
    return `${(b / 1048576).toFixed(1)}MB`;
  };

  return (
    <>
      <SectionTitle icon={<Database size={15} />} text="图片缓存" />
      <Card className="baoyu-network">
        <div className="baoyu-network-refresh" style={{ justifyContent: 'space-between' }}>
          <span className="baoyu-net-label">
            已缓存 {cache?.count ?? '—'} 张 · {fmtSize(cache?.size_bytes)}
          </span>
          <button
            type="button"
            className="baoyu-btn danger"
            onClick={onClearCache}
            disabled={cacheBusy}
          >
            <Trash2 size={14} />
            <span>{cacheBusy ? '清理中…' : '清空缓存'}</span>
          </button>
        </div>
        <div className="baoyu-net-hint">
          平台封面/主播头像本地持久化，首次拉取后不再请求上游。清空后下次访问重新拉取。
        </div>
      </Card>
    </>
  );
}

/* ────────────────────────── 4. 录制模式 (Web / 原生) ────────────────────────── */

function RecordSection() {
  const [modes, setModes] = useState<SettingsAll['modes'] | null>(null);
  const [modeSaving, setModeSaving] = useState(false);
  const [tab, setTab] = useState<'web' | 'native'>('web');

  const load = useCallback(async () => {
    try {
      const s = await fetchSettingsAll();
      setModes(s.modes);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onSetMode = async (updates: Parameters<typeof setMode>[0]) => {
    setModeSaving(true);
    try {
      const r = await setMode(updates);
      setModes(r);
      showToast('已保存（新任务生效）', 'success');
    } catch {
      showToast('保存失败', 'error');
    } finally {
      setModeSaving(false);
    }
  };

  return (
    <>
      {/* 顶部左右对称 Tab：Web 录制 / 原生录制 */}
      <Card className="baoyu-network">
        <div className="baoyu-record-tabs">
          <button
            type="button"
            className={`baoyu-record-tab ${tab === 'web' ? 'on' : ''}`}
            onClick={() => setTab('web')}
          >
            <Globe size={16} />
            <span>Web 录制</span>
          </button>
          <button
            type="button"
            className={`baoyu-record-tab ${tab === 'native' ? 'on' : ''}`}
            onClick={() => setTab('native')}
          >
            <Smartphone size={16} />
            <span>原生录制</span>
          </button>
        </div>
      </Card>

      {tab === 'web' ? (
        <>
          {/* Web 录制 */}
          <SectionTitle icon={<Globe size={15} />} text="Web 录制" />
          <Card className="baoyu-network">
            {/* 画质档位: 高/中/低 → 码率+帧率映射 */}
            <div className="baoyu-switch-row">
              <span className="baoyu-net-label">画质</span>
              <div className="baoyu-quality-seg">
                {([
                  { key: 'high', label: '高画质' },
                  { key: 'medium', label: '中画质' },
                  { key: 'low', label: '低画质' },
                ] as const).map((it) => (
                  <button
                    key={it.key}
                    type="button"
                    className={`baoyu-quality-btn ${modes?.quality === it.key ? 'on' : ''}`}
                    disabled={modeSaving || !modes}
                    onClick={() => onSetMode({ quality: it.key })}
                  >
                    {it.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="baoyu-switch-row">
              <span className="baoyu-net-label">h264 直出</span>
              <span className="baoyu-net-value">
                {modes?.transcode_fast ? '直通' : '实时转码'}
              </span>
              <button
                type="button"
                className={`baoyu-switch ${modes?.transcode_fast ? 'on' : ''}`}
                onClick={() => onSetMode({ transcode_fast: !modes?.transcode_fast })}
                disabled={modeSaving || !modes}
                aria-label="切换转码"
              >
                <span className="baoyu-switch-knob" />
              </button>
            </div>
            <div className="baoyu-switch-row">
              <span className="baoyu-net-label">码率 (bps)</span>
              <span className="baoyu-net-value">{(modes?.video_bitrate ?? 0) / 1000000}M</span>
            </div>
            <div className="baoyu-switch-row">
              <span className="baoyu-net-label">帧率 (fps)</span>
              <span className="baoyu-net-value">{modes?.capture_fps ?? 0}</span>
            </div>
            <p className="baoyu-mode-note">
              浏览器拉流 · 适用半糖 / 密语 / 番茄 / 小妲己 / Hot51 / swag（HLS 与加密通道）
            </p>
          </Card>

          {/* 运行参数（并入录制模式） */}
          <SectionTitle icon={<Cpu size={15} />} text="运行参数" />
          <Card className="baoyu-network">
            <div className="baoyu-switch-row">
              <span className="baoyu-net-label">单浏览器多 Tab</span>
              <span className="baoyu-net-value">
                {modes?.single_browser ? '省资源' : '独立进程'}
              </span>
              <button
                type="button"
                className={`baoyu-switch ${modes?.single_browser ? 'on' : ''}`}
                onClick={() => onSetMode({ single_browser: !modes?.single_browser })}
                disabled={modeSaving || !modes}
                aria-label="切换单浏览器"
              >
                <span className="baoyu-switch-knob" />
              </button>
            </div>
            <div className="baoyu-switch-row">
              <span className="baoyu-net-label">最大并发</span>
              <input
                className="baoyu-input baoyu-num"
                type="number"
                value={modes?.max_concurrent ?? ''}
                onChange={(e) => {
                  const v = parseInt(e.target.value, 10);
                  if (!Number.isNaN(v)) setModes((m) => (m ? { ...m, max_concurrent: v } : m));
                }}
                onBlur={() => {
                  if (modes && modes.max_concurrent) onSetMode({ max_concurrent: modes.max_concurrent });
                }}
                inputMode="numeric"
              />
            </div>
          </Card>
        </>
      ) : (
        <>
          {/* 直录 ffmpeg（FLV 源流平台） */}
          <SectionTitle icon={<Video size={15} />} text="直录 ffmpeg" />
          <Card className="baoyu-network">
            <div className="baoyu-net-row">
              <span className="baoyu-net-icon on"><Video size={14} /></span>
              <span className="baoyu-net-label">录制方式</span>
              <span className="baoyu-net-value on">ffmpeg 直拉源流</span>
            </div>
            <div className="baoyu-net-row">
              <span className="baoyu-net-icon on"><Zap size={14} /></span>
              <span className="baoyu-net-label">转码</span>
              <span className="baoyu-net-value on">零转码 · 流封装</span>
            </div>
            <div className="baoyu-net-row">
              <span className="baoyu-net-icon on"><MonitorOff size={14} /></span>
              <span className="baoyu-net-label">浏览器开销</span>
              <span className="baoyu-net-value on">无（CPU ≈ 0%）</span>
            </div>
            <p className="baoyu-mode-note">
              服务端 ffmpeg 直接拉 FLV 源流封装成 MP4 · 适用杏仁 / 青瑶 / 草莓（直出 FLV 平台）
            </p>
          </Card>

          {/* 原生录制（未实现） */}
          <SectionTitle icon={<Smartphone size={15} />} text="原生录制" />
          <Card className="baoyu-network baoyu-soon">
            <div className="baoyu-soon-inner">
              <Smartphone size={28} />
              <span className="baoyu-soon-title">原生录制</span>
              <span className="baoyu-soon-desc">正在开发 · 敬请期待</span>
            </div>
          </Card>
        </>
      )}
    </>
  );
}

/* ────────────────────────── 公共小组件 ────────────────────────── */

function SectionTitle({ icon, text }: { icon: React.ReactNode; text: string }) {
  return (
    <div className="baoyu-settings-title">
      {icon}
      <span>{text}</span>
    </div>
  );
}

function NetRow({
  icon,
  label,
  value,
  ok,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  ok?: boolean;
}) {
  return (
    <div className="baoyu-net-row">
      <span className={`baoyu-net-icon ${ok ? '' : 'off'}`}>{icon}</span>
      <span className="baoyu-net-label">{label}</span>
      <span className={`baoyu-net-value ${ok ? '' : 'off'}`}>{value}</span>
    </div>
  );
}
