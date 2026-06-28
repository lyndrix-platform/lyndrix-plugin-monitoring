import { useState, useEffect, useCallback, useRef, useMemo, CSSProperties } from 'react'
// react-i18next / i18next are provided by the host shell
// (window.__lyndrix_react_i18next / window.__lyndrix_i18n); declared external in
// vite.ui.config.ts so the plugin shares the host's i18n instance + active
// language. Strings come from locales/monitoring.<locale>.json, auto-registered
// by core and served via the catalog (namespace "monitoring").
import { useTranslation } from 'react-i18next'
import { pluginApi } from './lib/api'

// ─── Types ────────────────────────────────────────────────────────────────────

type MonitorState = 'UP' | 'DOWN' | 'PAUSED' | 'UNKNOWN'

interface ServiceView {
  monitor_id: string
  name: string | null
  display_name: string
  state: MonitorState
  uptime_24h: number
  uptime_all: number
  timeline: MonitorState[]
  target: string | null
  type: string | null
  latest_error: string | null
}

interface Host {
  name: string
  address: string | null
  stage: string | null
  state: MonitorState
  timeline: MonitorState[]
  uptime_24h: number
  uptime_all: number
  host_monitor: ServiceView | null
  services: ServiceView[]
  service_count: number
}

interface Group {
  name: string | null
  icon: string
  state: MonitorState
  timeline: MonitorState[]
  uptime_24h: number
  uptime_all: number
  hosts: Host[]
  host_count: number
  service_count: number
}

interface Row {
  monitor_id: string
  name: string
  type: string
  location: string
  host: string
  host_key: string
  site: string
  stage: string
  state: MonitorState
  uptime_24h: number
  uptime_all: number
  timeline: MonitorState[]
  target: string | null
  latest_error: string | null
}

interface Stats {
  monitor_count: number
  up_count: number
  down_count: number
  paused_count: number
  uptime_all: number
}

interface Overview {
  groups: Group[]
  rows: Row[]
  stats: Stats
  hours: number
}

// ─── Prefs (localStorage, prefix lyndrix_monitoring.) ───────────────────────────

type ViewMode = 'cards' | 'table' | 'split'
type GroupBy = 'site' | 'stage' | 'location' | 'status' | 'flat'
type Density = 'compact' | 'cozy' | 'spacious'

interface Prefs {
  view_mode: ViewMode
  group_by: GroupBy
  density: Density
  show_timelines: boolean
  show_uptime_24h: boolean
  show_uptime_all: boolean
  show_services_in_host: boolean
  show_paused: boolean
  show_unknown: boolean
}

const DEFAULT_PREFS: Prefs = {
  view_mode: 'cards',
  group_by: 'site',
  density: 'cozy',
  show_timelines: true,
  show_uptime_24h: true,
  show_uptime_all: true,
  show_services_in_host: true,
  show_paused: true,
  show_unknown: true,
}

const PREFS_PREFIX = 'lyndrix_monitoring.'

function loadPrefs(): Prefs {
  const p = { ...DEFAULT_PREFS }
  for (const key of Object.keys(DEFAULT_PREFS) as (keyof Prefs)[]) {
    const raw = localStorage.getItem(PREFS_PREFIX + key)
    if (raw === null) continue
    try {
      ;(p as Record<string, unknown>)[key] = JSON.parse(raw)
    } catch {
      /* ignore malformed pref */
    }
  }
  return p
}

function persistPref<K extends keyof Prefs>(key: K, value: Prefs[K]): void {
  try {
    localStorage.setItem(PREFS_PREFIX + key, JSON.stringify(value))
  } catch {
    /* storage unavailable — keep in-memory only */
  }
}

// ─── Colours / shared styles ────────────────────────────────────────────────────

const STATE_COLOR: Record<MonitorState, string> = {
  UP: 'var(--lx-state-up)',
  DOWN: 'var(--lx-state-down)',
  PAUSED: 'var(--lx-state-paused)',
  UNKNOWN: 'var(--lx-state-unknown)',
}

function stateColor(state: string): string {
  return STATE_COLOR[(state as MonitorState)] ?? STATE_COLOR.UNKNOWN
}

const GLASS: CSSProperties = {
  background: 'var(--lx-surface-glass, var(--lx-surface))',
  backdropFilter: 'blur(16px) saturate(160%)',
  WebkitBackdropFilter: 'blur(16px) saturate(160%)',
  border: '1px solid var(--lx-glass-border, var(--lx-border-soft))',
}

function fmtPct(v: number): string {
  return (v ?? 0).toFixed(1)
}

// ─── Timeline (port of app/ui/timeline.py) ──────────────────────────────────────

type TimelineSize = 'full' | 'host' | 'service'

interface SizeCfg {
  heights: Record<MonitorState, number>
  container: number
  gap: string
  radius: string
  tickHeight: string
  scaleFont: string
  scalePadding: string
  scaleMargin: string
}

const SIZE_CONFIG: Record<TimelineSize, SizeCfg> = {
  full: {
    heights: { UP: 80, DOWN: 40, PAUSED: 44, UNKNOWN: 20 },
    container: 88,
    gap: '6px',
    radius: '0 0 3px 3px',
    tickHeight: '8px',
    scaleFont: '10px',
    scalePadding: '5px',
    scaleMargin: '3px',
  },
  host: {
    heights: { UP: 46, DOWN: 23, PAUSED: 26, UNKNOWN: 12 },
    container: 52,
    gap: '4px',
    radius: '0 0 2px 2px',
    tickHeight: '6px',
    scaleFont: '9px',
    scalePadding: '3px',
    scaleMargin: '2px',
  },
  service: {
    heights: { UP: 30, DOWN: 15, PAUSED: 16, UNKNOWN: 6 },
    container: 34,
    gap: '2px',
    radius: '0 0 1px 1px',
    tickHeight: '4px',
    scaleFont: '8px',
    scalePadding: '2px',
    scaleMargin: '1px',
  },
}

function Timeline({ states, size }: { states: MonitorState[]; size: TimelineSize }) {
  const cfg = SIZE_CONFIG[size]
  const tl = states && states.length ? states : (Array(24).fill('UNKNOWN') as MonitorState[])
  const total = tl.length
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${total},minmax(0,1fr))`,
        gap: cfg.gap,
        width: '100%',
        height: cfg.container,
        boxSizing: 'border-box',
      }}
    >
      {tl.map((state, index) => {
        const color = stateColor(state)
        const height = cfg.heights[state] ?? cfg.heights.UNKNOWN
        return (
          <div
            key={index}
            title={`-${total - index}h: ${state}`}
            style={{
              height,
              borderRadius: cfg.radius,
              background: color,
              opacity: 0.95,
              boxShadow: `0 0 6px ${color}22`,
              alignSelf: 'end',
            }}
          />
        )
      })}
    </div>
  )
}

function TimelineScale({ size, hours = 24 }: { size: TimelineSize; hours?: number }) {
  const { t } = useTranslation('monitoring')
  const cfg = SIZE_CONFIG[size]
  const tickPositions: Record<number, string> = {
    0: `-${hours}h`,
    [Math.max(0, Math.floor(hours / 2) - 1)]: `-${Math.floor(hours / 2)}h`,
    [Math.max(0, hours - 6 - 1)]: '-6h',
    [hours - 1]: t('timeline.now', { defaultValue: 'now' }),
  }
  const gridStyle: CSSProperties = {
    display: 'grid',
    gridTemplateColumns: `repeat(${hours},minmax(0,1fr))`,
    gap: cfg.gap,
    width: '100%',
  }
  const ticks = []
  const labels = []
  for (let i = 0; i < hours; i++) {
    const isTick = i in tickPositions
    ticks.push(
      <div
        key={i}
        style={{
          width: 1,
          height: cfg.tickHeight,
          margin: '0 auto',
          background: isTick
            ? 'rgba(var(--lx-text-muted-raw,161,161,170),0.7)'
            : 'rgba(82,82,91,0.25)',
        }}
      />,
    )
    const label = tickPositions[i]
    if (label) {
      const transform = i === 0 ? 'translateX(0%)' : i === hours - 1 ? 'translateX(-100%)' : 'translateX(-50%)'
      labels.push(
        <div
          key={i}
          style={{
            fontSize: cfg.scaleFont,
            color: 'var(--lx-text-muted)',
            letterSpacing: '0.1em',
            whiteSpace: 'nowrap',
            transform,
          }}
        >
          {label}
        </div>,
      )
    } else {
      labels.push(<div key={i} />)
    }
  }
  return (
    <div style={{ width: '100%', paddingTop: cfg.scalePadding }}>
      <div style={{ ...gridStyle, height: cfg.tickHeight }}>{ticks}</div>
      <div style={{ ...gridStyle, marginTop: cfg.scaleMargin }}>{labels}</div>
    </div>
  )
}

// ─── State badge ────────────────────────────────────────────────────────────────

const STATE_LABEL: Record<MonitorState, string> = {
  UP: 'Online',
  DOWN: 'Offline',
  PAUSED: 'Paused',
  UNKNOWN: 'Unknown',
}

function StateBadge({ state, small }: { state: MonitorState; small?: boolean }) {
  const { t } = useTranslation('monitoring')
  const color = stateColor(state)
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontSize: small ? '0.6rem' : '0.68rem',
        fontWeight: 700,
        color,
        background: `color-mix(in srgb, ${color} 12%, transparent)`,
        border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
        borderRadius: '999px',
        padding: small ? '1px 6px' : '2px 8px',
        letterSpacing: '0.16em',
        textTransform: 'uppercase',
        whiteSpace: 'nowrap',
        flexShrink: 0,
      }}
    >
      <span style={{ width: small ? 5 : 6, height: small ? 5 : 6, borderRadius: '50%', background: color, flexShrink: 0 }} />
      {t(`state.${state.toLowerCase()}`, { defaultValue: STATE_LABEL[state] ?? state })}
    </span>
  )
}

// ─── Density tokens ─────────────────────────────────────────────────────────────

const DENSITY_PAD: Record<Density, { padding: string; gap: number }> = {
  compact: { padding: '8px', gap: 6 },
  cozy: { padding: '16px', gap: 12 },
  spacious: { padding: '20px', gap: 16 },
}
const DENSITY_TITLE: Record<Density, string> = {
  compact: '0.875rem',
  cozy: '1rem',
  spacious: '1.125rem',
}
const DENSITY_ROW: Record<Density, { fontSize: string; padding: string }> = {
  compact: { fontSize: '0.75rem', padding: '4px 10px' },
  cozy: { fontSize: '0.85rem', padding: '7px 10px' },
  spacious: { fontSize: '0.85rem', padding: '11px 12px' },
}

// ─── Host card (port of _components/host_card.py) ────────────────────────────────

function ServicesInline({ host, prefs }: { host: Host; prefs: Prefs }) {
  if (!host.services || host.services.length === 0) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, width: '100%' }}>
      {host.services.map((svc) => (
        <div
          key={svc.monitor_id}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            width: '100%',
            paddingTop: 6,
            borderTop: '1px solid var(--lx-border-soft)',
          }}
        >
          <span
            style={{
              fontSize: '0.75rem',
              fontWeight: 700,
              color: 'var(--lx-text)',
              flex: 1,
              minWidth: 0,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {svc.display_name}
          </span>
          {prefs.show_uptime_24h && (
            <span style={{ fontSize: '0.68rem', fontFamily: 'monospace', color: 'var(--lx-text-muted)', flexShrink: 0 }}>
              {fmtPct(svc.uptime_24h)}%
            </span>
          )}
          <StateBadge state={svc.state} small />
        </div>
      ))}
    </div>
  )
}

function HostHeader({ host, prefs }: { host: Host; prefs: Prefs }) {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 8, width: '100%' }}>
      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, flex: 1 }}>
        <span
          style={{
            fontSize: DENSITY_TITLE[prefs.density],
            fontWeight: 800,
            color: 'var(--lx-text)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {host.name}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
          {host.address && (
            <span
              style={{
                fontSize: '0.72rem',
                fontFamily: 'monospace',
                color: 'var(--lx-text-muted)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {host.address}
            </span>
          )}
          {host.stage && host.stage !== 'General' && (
            <span
              style={{
                fontSize: '0.6rem',
                textTransform: 'uppercase',
                letterSpacing: '0.16em',
                padding: '1px 7px',
                borderRadius: '999px',
                background: 'color-mix(in srgb, var(--lx-text-muted) 12%, transparent)',
                border: '1px solid var(--lx-border-soft)',
                color: 'var(--lx-text-muted)',
                flexShrink: 0,
              }}
            >
              {host.stage}
            </span>
          )}
        </div>
      </div>
      <StateBadge state={host.state} />
    </div>
  )
}

function HostMeta({ host, prefs }: { host: Host; prefs: Prefs }) {
  const { t } = useTranslation('monitoring')
  const hasServices = host.services && host.services.length > 0
  const left = hasServices
    ? t('host.service', { count: host.service_count, defaultValue: '{{count}} services' })
    : t('host.standalone', { defaultValue: 'Standalone host' })
  const right: string[] = []
  if (prefs.show_uptime_24h) right.push(t('host.uptime_24h', { value: fmtPct(host.uptime_24h), defaultValue: '24h {{value}}%' }))
  if (prefs.show_uptime_all) right.push(t('host.uptime_all', { value: fmtPct(host.uptime_all), defaultValue: 'all-time {{value}}%' }))
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%' }}>
      <span style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>{left}</span>
      {right.length > 0 && <span style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>{right.join(' · ')}</span>}
    </div>
  )
}

function HostCard({ host, prefs }: { host: Host; prefs: Prefs }) {
  const color = stateColor(host.state)
  const pad = DENSITY_PAD[prefs.density]
  return (
    <div
      style={{
        ...GLASS,
        borderRadius: 'var(--lx-radius-md)',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
      }}
    >
      <div style={{ height: 4, width: '100%', background: color, boxShadow: `0 0 18px ${color}66` }} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: pad.gap, padding: pad.padding, width: '100%' }}>
        <HostHeader host={host} prefs={prefs} />
        <HostMeta host={host} prefs={prefs} />
        {prefs.show_timelines && (
          <div style={{ width: '100%' }}>
            <Timeline states={host.timeline} size="host" />
            <TimelineScale size="host" />
          </div>
        )}
        {prefs.show_services_in_host && <ServicesInline host={host} prefs={prefs} />}
      </div>
    </div>
  )
}

function HostCardDetail({ host, prefs }: { host: Host; prefs: Prefs }) {
  const { t } = useTranslation('monitoring')
  const color = stateColor(host.state)
  return (
    <div
      id="lx-mon-detail"
      style={{ ...GLASS, borderRadius: 'var(--lx-radius-md)', overflow: 'hidden', display: 'flex', flexDirection: 'column', width: '100%' }}
    >
      <div style={{ height: 4, width: '100%', background: color, boxShadow: `0 0 18px ${color}66` }} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 20, width: '100%' }}>
        <HostHeader host={host} prefs={prefs} />
        <HostMeta host={host} prefs={prefs} />
        <div style={{ width: '100%' }}>
          <Timeline states={host.timeline} size="full" />
          <TimelineScale size="full" />
        </div>
        {host.host_monitor && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.16em', color: 'var(--lx-text-muted)', fontWeight: 700 }}>
              {t('host.host_monitor', { defaultValue: 'Host monitor' })}
            </span>
            <StateBadge state={host.host_monitor.state} small />
            {host.host_monitor.target && (
              <span style={{ fontSize: '0.72rem', fontFamily: 'monospace', color: 'var(--lx-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {host.host_monitor.target}
              </span>
            )}
          </div>
        )}
        {host.services && host.services.length > 0 && (
          <>
            <div style={{ borderTop: '1px solid var(--lx-border-soft)' }} />
            <ServicesInline host={host} prefs={prefs} />
          </>
        )}
      </div>
    </div>
  )
}

// ─── Group header (port of _components/group_header.py) ──────────────────────────

function GroupHeader({ group, prefs }: { group: Group; prefs: Prefs }) {
  const { t } = useTranslation('monitoring')
  if (group.name === null) return null
  const uptime: string[] = []
  if (prefs.show_uptime_24h) uptime.push(t('host.uptime_24h', { value: fmtPct(group.uptime_24h), defaultValue: '24h {{value}}%' }))
  if (prefs.show_uptime_all) uptime.push(t('host.uptime_all', { value: fmtPct(group.uptime_all), defaultValue: 'all-time {{value}}%' }))
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        width: '100%',
        paddingBottom: 8,
        borderBottom: '1px solid var(--lx-border-soft)',
      }}
    >
      <span
        style={{
          fontSize: '1.1rem',
          fontWeight: 900,
          letterSpacing: '0.06em',
          color: 'var(--lx-text)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {group.name}
      </span>
      <span style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
        {t('group.summary', { hosts: group.host_count, services: group.service_count, defaultValue: '{{hosts}} hosts · {{services}} services' })}
      </span>
      <span style={{ flex: 1 }} />
      <StateBadge state={group.state} />
      {uptime.length > 0 && <span style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>{uptime.join(' · ')}</span>}
    </div>
  )
}

// ─── Empty state ────────────────────────────────────────────────────────────────

function EmptyCard({ title, hint }: { title: string; hint?: string }) {
  return (
    <div style={{ ...GLASS, borderRadius: 'var(--lx-radius-md)', padding: '2rem', textAlign: 'center', width: '100%' }}>
      <div style={{ fontSize: '2.5rem', opacity: 0.4, marginBottom: 8 }}>📡</div>
      <div style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--lx-text)' }}>{title}</div>
      {hint && <div style={{ fontSize: '0.85rem', color: 'var(--lx-text-muted)', marginTop: 4 }}>{hint}</div>}
    </div>
  )
}

// ─── Cards view ─────────────────────────────────────────────────────────────────

function CardsView({ groups, prefs }: { groups: Group[]; prefs: Prefs }) {
  const { t } = useTranslation('monitoring')
  if (groups.length === 0) {
    return (
      <EmptyCard
        title={t('empty.title', { defaultValue: 'No monitors match the current filters.' })}
        hint={t('empty.hint', { defaultValue: 'Try enabling paused/unknown items or check the monitor registry.' })}
      />
    )
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24, width: '100%' }}>
      {groups.map((group, gi) => (
        <div key={group.name ?? `flat-${gi}`} style={{ display: 'flex', flexDirection: 'column', gap: 16, width: '100%' }}>
          <GroupHeader group={group} prefs={prefs} />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(350px, 1fr))', gap: 16, width: '100%' }}>
            {group.hosts.map((host) => (
              <HostCard key={host.name + (host.address ?? '')} host={host} prefs={prefs} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

// ─── Table view ─────────────────────────────────────────────────────────────────

type SortKey = 'name' | 'type' | 'location' | 'host' | 'state' | 'uptime_24h' | 'uptime_all'

function TableView({ rows, prefs }: { rows: Row[]; prefs: Prefs }) {
  const { t } = useTranslation('monitoring')
  const [sortKey, setSortKey] = useState<SortKey>('name')
  const [sortDir, setSortDir] = useState<1 | -1>(1)

  const sorted = useMemo(() => {
    const out = [...rows]
    out.sort((a, b) => {
      const av = a[sortKey]
      const bv = b[sortKey]
      let cmp: number
      if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv
      else cmp = String(av).localeCompare(String(bv))
      return cmp * sortDir
    })
    return out
  }, [rows, sortKey, sortDir])

  if (rows.length === 0) {
    return <EmptyCard title={t('empty.title', { defaultValue: 'No monitors match the current filters.' })} />
  }

  const onSort = (key: SortKey) => {
    if (key === sortKey) setSortDir((d) => (d === 1 ? -1 : 1))
    else {
      setSortKey(key)
      setSortDir(1)
    }
  }

  const rowCss = DENSITY_ROW[prefs.density]
  const headerStyle: CSSProperties = {
    fontSize: '0.7rem',
    textTransform: 'uppercase',
    letterSpacing: '0.1em',
    color: 'var(--lx-text-muted)',
    fontWeight: 700,
    padding: rowCss.padding,
    textAlign: 'left',
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  }
  const cellStyle: CSSProperties = {
    fontSize: rowCss.fontSize,
    color: 'var(--lx-text)',
    padding: rowCss.padding,
    borderTop: '1px solid var(--lx-border-soft)',
    whiteSpace: 'nowrap',
  }
  const cols: { key: SortKey; label: string; align?: 'left' | 'right' }[] = [
    { key: 'name', label: t('table.name', { defaultValue: 'Name' }) },
    { key: 'type', label: t('table.type', { defaultValue: 'Type' }) },
    { key: 'location', label: t('table.location', { defaultValue: 'Location' }) },
    { key: 'host', label: t('table.host', { defaultValue: 'Host' }) },
    { key: 'state', label: t('table.status', { defaultValue: 'Status' }) },
  ]
  const arrow = (key: SortKey) => (key === sortKey ? (sortDir === 1 ? ' ▲' : ' ▼') : '')

  return (
    <div style={{ ...GLASS, borderRadius: 'var(--lx-radius-md)', overflowX: 'auto', width: '100%' }}>
      <table style={{ width: '100%', minWidth: 720, borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.key} style={headerStyle} onClick={() => onSort(c.key)}>
                {c.label}
                {arrow(c.key)}
              </th>
            ))}
            {prefs.show_uptime_24h && (
              <th style={{ ...headerStyle, textAlign: 'right' }} onClick={() => onSort('uptime_24h')}>
                {t('table.uptime_24h', { defaultValue: '24h' })}
                {arrow('uptime_24h')}
              </th>
            )}
            {prefs.show_uptime_all && (
              <th style={{ ...headerStyle, textAlign: 'right' }} onClick={() => onSort('uptime_all')}>
                {t('table.uptime_all', { defaultValue: 'All-time' })}
                {arrow('uptime_all')}
              </th>
            )}
            {prefs.show_timelines && <th style={{ ...headerStyle, cursor: 'default' }}>{t('table.timeline', { defaultValue: 'Timeline' })}</th>}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => (
            <tr key={row.monitor_id}>
              <td style={{ ...cellStyle, fontWeight: 700 }}>{row.name}</td>
              <td style={{ ...cellStyle, color: 'var(--lx-text-muted)' }}>{row.type}</td>
              <td style={{ ...cellStyle, color: 'var(--lx-text-muted)' }}>{row.location}</td>
              <td style={{ ...cellStyle, fontFamily: 'monospace', color: 'var(--lx-text-muted)' }}>{row.host}</td>
              <td style={cellStyle}>
                <StateBadge state={row.state} small />
              </td>
              {prefs.show_uptime_24h && (
                <td style={{ ...cellStyle, textAlign: 'right', fontFamily: 'monospace' }}>{fmtPct(row.uptime_24h)}%</td>
              )}
              {prefs.show_uptime_all && (
                <td style={{ ...cellStyle, textAlign: 'right', fontFamily: 'monospace' }}>{fmtPct(row.uptime_all)}%</td>
              )}
              {prefs.show_timelines && (
                <td style={{ ...cellStyle, minWidth: 160 }}>
                  <Timeline states={row.timeline} size="service" />
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ─── Split view ─────────────────────────────────────────────────────────────────

function findHost(groups: Group[], hostKey: string | null): Host | null {
  if (!hostKey) return null
  for (const g of groups) {
    for (const h of g.hosts) {
      if (h.name === hostKey || h.address === hostKey) return h
    }
  }
  return null
}

function SplitView({ rows, groups, prefs }: { rows: Row[]; groups: Group[]; prefs: Prefs }) {
  const { t } = useTranslation('monitoring')
  const [selected, setSelected] = useState<string | null>(null)

  // Default-select the first row whenever the data set changes.
  useEffect(() => {
    setSelected((cur) => {
      if (cur && rows.some((r) => r.host_key === cur)) return cur
      return rows.length ? rows[0].host_key : null
    })
  }, [rows])

  if (rows.length === 0) {
    return <EmptyCard title={t('empty.title', { defaultValue: 'No monitors match the current filters.' })} />
  }

  const host = findHost(groups, selected)
  const rowCss = DENSITY_ROW[prefs.density]

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 420px) 1fr', gap: 16, width: '100%' }} className="lx-mon-split">
      {/* Left pane: compact selectable list */}
      <div style={{ ...GLASS, borderRadius: 'var(--lx-radius-md)', overflowX: 'auto', alignSelf: 'start' }}>
        <table style={{ width: '100%', minWidth: 360, borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {[t('table.name', { defaultValue: 'Name' }), t('table.host', { defaultValue: 'Host' }), t('table.status', { defaultValue: 'Status' }), t('table.uptime_24h', { defaultValue: '24h' })].map(
                (label, i) => (
                  <th
                    key={i}
                    style={{
                      fontSize: '0.68rem',
                      textTransform: 'uppercase',
                      letterSpacing: '0.1em',
                      color: 'var(--lx-text-muted)',
                      fontWeight: 700,
                      padding: rowCss.padding,
                      textAlign: i === 3 ? 'right' : 'left',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {label}
                  </th>
                ),
              )}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const active = row.host_key === selected
              return (
                <tr
                  key={row.monitor_id}
                  onClick={() => setSelected(row.host_key)}
                  style={{ cursor: 'pointer', background: active ? 'color-mix(in srgb, var(--lx-accent) 12%, transparent)' : 'transparent' }}
                >
                  <td
                    style={{
                      fontSize: rowCss.fontSize,
                      fontWeight: 700,
                      color: 'var(--lx-text)',
                      padding: rowCss.padding,
                      borderTop: '1px solid var(--lx-border-soft)',
                      borderLeft: active ? '2px solid var(--lx-accent)' : '2px solid transparent',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {row.name}
                  </td>
                  <td style={{ fontSize: rowCss.fontSize, fontFamily: 'monospace', color: 'var(--lx-text-muted)', padding: rowCss.padding, borderTop: '1px solid var(--lx-border-soft)', whiteSpace: 'nowrap' }}>
                    {row.host}
                  </td>
                  <td style={{ padding: rowCss.padding, borderTop: '1px solid var(--lx-border-soft)' }}>
                    <StateBadge state={row.state} small />
                  </td>
                  <td style={{ fontSize: rowCss.fontSize, fontFamily: 'monospace', color: 'var(--lx-text-muted)', padding: rowCss.padding, borderTop: '1px solid var(--lx-border-soft)', textAlign: 'right', whiteSpace: 'nowrap' }}>
                    {fmtPct(row.uptime_24h)}%
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Right pane: detail card */}
      <div style={{ width: '100%' }}>
        {host ? (
          <HostCardDetail host={host} prefs={prefs} />
        ) : (
          <EmptyCard
            title={t('split.select_title', { defaultValue: 'Select a row' })}
            hint={t('split.select_hint', { defaultValue: 'Click an entry on the left to see its timeline + services.' })}
          />
        )}
      </div>
    </div>
  )
}

// ─── Toolbar ────────────────────────────────────────────────────────────────────

function Toolbar({
  prefs,
  setPref,
  onRefresh,
}: {
  prefs: Prefs
  setPref: <K extends keyof Prefs>(key: K, value: Prefs[K]) => void
  onRefresh: () => void
}) {
  const { t } = useTranslation('monitoring')
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menuOpen) return
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [menuOpen])

  const viewModes: { value: ViewMode; label: string }[] = [
    { value: 'cards', label: t('toolbar.view_cards', { defaultValue: 'Cards' }) },
    { value: 'table', label: t('toolbar.view_table', { defaultValue: 'Table' }) },
    { value: 'split', label: t('toolbar.view_split', { defaultValue: 'Split' }) },
  ]
  const groupByOptions: { value: GroupBy; label: string }[] = [
    { value: 'site', label: t('toolbar.group_site', { defaultValue: 'Site' }) },
    { value: 'stage', label: t('toolbar.group_stage', { defaultValue: 'Stage' }) },
    { value: 'location', label: t('toolbar.group_location', { defaultValue: 'Location' }) },
    { value: 'status', label: t('toolbar.group_status', { defaultValue: 'Status' }) },
    { value: 'flat', label: t('toolbar.group_flat', { defaultValue: 'Flat (no grouping)' }) },
  ]
  const densityOptions: { value: Density; label: string }[] = [
    { value: 'compact', label: t('toolbar.density_compact', { defaultValue: 'Compact' }) },
    { value: 'cozy', label: t('toolbar.density_cozy', { defaultValue: 'Cozy' }) },
    { value: 'spacious', label: t('toolbar.density_spacious', { defaultValue: 'Spacious' }) },
  ]
  const toggles: { field: keyof Prefs; label: string; sep?: boolean }[] = [
    { field: 'show_timelines', label: t('toolbar.toggle_timelines', { defaultValue: 'Timelines' }) },
    { field: 'show_uptime_24h', label: t('toolbar.toggle_uptime_24h', { defaultValue: '24h uptime' }) },
    { field: 'show_uptime_all', label: t('toolbar.toggle_uptime_all', { defaultValue: 'All-time uptime' }) },
    { field: 'show_services_in_host', label: t('toolbar.toggle_services', { defaultValue: 'Services in host' }) },
    { field: 'show_paused', label: t('toolbar.toggle_paused', { defaultValue: 'Paused monitors' }), sep: true },
    { field: 'show_unknown', label: t('toolbar.toggle_unknown', { defaultValue: 'Unknown monitors' }) },
  ]

  const selectStyle: CSSProperties = {
    ...GLASS,
    borderRadius: 'var(--lx-radius-sm)',
    color: 'var(--lx-text)',
    fontSize: '0.78rem',
    padding: '6px 10px',
    cursor: 'pointer',
  }
  const labelStyle: CSSProperties = { fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.1em', color: 'var(--lx-text-muted)', fontWeight: 700 }

  return (
    <div style={{ ...GLASS, borderRadius: 'var(--lx-radius-md)', padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
      {/* View-mode segmented control */}
      <div style={{ display: 'inline-flex', gap: 4, padding: 4, borderRadius: 'var(--lx-radius-sm)', background: 'color-mix(in srgb, var(--lx-text-muted) 8%, transparent)', border: '1px solid var(--lx-border-soft)' }}>
        {viewModes.map((vm) => {
          const active = prefs.view_mode === vm.value
          return (
            <button
              key={vm.value}
              onClick={() => setPref('view_mode', vm.value)}
              style={{
                padding: '4px 12px',
                borderRadius: '6px',
                fontSize: '0.74rem',
                fontWeight: 700,
                cursor: 'pointer',
                border: active ? '1px solid color-mix(in srgb, var(--lx-accent) 35%, transparent)' : '1px solid transparent',
                background: active ? 'color-mix(in srgb, var(--lx-accent) 16%, transparent)' : 'transparent',
                color: active ? 'var(--lx-accent)' : 'var(--lx-text-muted)',
              }}
            >
              {vm.label}
            </button>
          )
        })}
      </div>

      {/* Group by */}
      <label style={{ display: 'inline-flex', flexDirection: 'column', gap: 3 }}>
        <span style={labelStyle}>{t('toolbar.group_by', { defaultValue: 'Group by' })}</span>
        <select value={prefs.group_by} onChange={(e) => setPref('group_by', e.target.value as GroupBy)} style={selectStyle}>
          {groupByOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>

      {/* Density */}
      <label style={{ display: 'inline-flex', flexDirection: 'column', gap: 3 }}>
        <span style={labelStyle}>{t('toolbar.density', { defaultValue: 'Density' })}</span>
        <select value={prefs.density} onChange={(e) => setPref('density', e.target.value as Density)} style={selectStyle}>
          {densityOptions.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>

      <span style={{ flex: 1 }} />

      {/* Visibility popover */}
      <div style={{ position: 'relative' }} ref={menuRef}>
        <button
          onClick={() => setMenuOpen((o) => !o)}
          title={t('toolbar.visibility', { defaultValue: 'Show / hide sections' })}
          style={{ ...selectStyle, padding: '8px 12px', fontWeight: 700 }}
        >
          ☰ {t('toolbar.visibility', { defaultValue: 'Show / hide sections' })}
        </button>
        {menuOpen && (
          <div
            style={{
              position: 'absolute',
              right: 0,
              top: 'calc(100% + 6px)',
              zIndex: 50,
              minWidth: 240,
              padding: 12,
              borderRadius: 'var(--lx-radius-md)',
              background: 'var(--lx-elevated, var(--lx-surface))',
              border: '1px solid var(--lx-glass-border, var(--lx-border-soft))',
              backdropFilter: 'blur(16px) saturate(160%)',
              WebkitBackdropFilter: 'blur(16px) saturate(160%)',
              boxShadow: 'var(--lx-glow, 0 8px 30px rgba(0,0,0,0.35))',
            }}
          >
            <div style={{ ...labelStyle, marginBottom: 8 }}>{t('toolbar.visibility_title', { defaultValue: 'Show columns & sections' })}</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {toggles.map((tg) => (
                <div key={tg.field}>
                  {tg.sep && <div style={{ borderTop: '1px solid var(--lx-border-soft)', margin: '6px 0' }} />}
                  <label style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, cursor: 'pointer' }}>
                    <span style={{ fontSize: '0.78rem', color: 'var(--lx-text)' }}>{tg.label}</span>
                    <input
                      type="checkbox"
                      checked={Boolean(prefs[tg.field])}
                      onChange={(e) => setPref(tg.field, e.target.checked as Prefs[typeof tg.field])}
                      style={{ accentColor: 'var(--lx-accent)', width: 16, height: 16, cursor: 'pointer' }}
                    />
                  </label>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Refresh */}
      <button onClick={onRefresh} title={t('toolbar.refresh', { defaultValue: 'Refresh now' })} style={{ ...selectStyle, padding: '8px 14px', fontWeight: 700 }}>
        ⟳ {t('app.refresh', { defaultValue: 'Refresh' })}
      </button>
    </div>
  )
}

// ─── Header card with 5-stat grid ───────────────────────────────────────────────

function HeaderCard({ stats }: { stats: Stats | null }) {
  const { t } = useTranslation('monitoring')
  const items: { key: keyof Stats; label: string; color: string; pct?: boolean }[] = [
    { key: 'monitor_count', label: t('hstat.monitors', { defaultValue: 'Monitors' }), color: 'var(--lx-accent)' },
    { key: 'up_count', label: t('hstat.up', { defaultValue: 'Up' }), color: STATE_COLOR.UP },
    { key: 'down_count', label: t('hstat.down', { defaultValue: 'Down' }), color: STATE_COLOR.DOWN },
    { key: 'paused_count', label: t('hstat.paused', { defaultValue: 'Paused' }), color: STATE_COLOR.PAUSED },
    { key: 'uptime_all', label: t('hstat.uptime', { defaultValue: 'Uptime' }), color: 'var(--lx-accent-2)', pct: true },
  ]
  return (
    <div style={{ ...GLASS, borderRadius: 'var(--lx-radius-lg, var(--lx-radius-md))', overflow: 'hidden', width: '100%' }}>
      <div style={{ height: 4, width: '100%', background: 'linear-gradient(90deg, var(--lx-accent), var(--lx-accent-2), var(--lx-accent-3))' }} />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: '24px' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: '1.4rem', fontWeight: 900, color: 'var(--lx-text)' }}>
            {t('header.title', { defaultValue: 'State Monitoring' })}
          </h1>
          <p style={{ margin: '4px 0 0', fontSize: '0.85rem', color: 'var(--lx-text-muted)', maxWidth: 640 }}>
            {t('header.description', {
              defaultValue: 'Persistent monitoring for servers and services with grouped status timelines and optional IaC inventory sync.',
            })}
          </p>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 12 }}>
          {items.map((it) => (
            <div
              key={it.key}
              style={{
                ...GLASS,
                borderRadius: 'var(--lx-radius-md)',
                padding: '14px',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                textAlign: 'center',
                gap: 2,
              }}
            >
              <span style={{ fontSize: '0.62rem', textTransform: 'uppercase', letterSpacing: '0.12em', color: 'var(--lx-text-muted)', fontWeight: 700 }}>
                {it.label}
              </span>
              <span style={{ fontSize: '1.9rem', fontWeight: 900, color: it.color, lineHeight: 1 }}>
                {stats ? (it.pct ? `${fmtPct(stats[it.key])}%` : stats[it.key]) : '—'}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Root ───────────────────────────────────────────────────────────────────────

export default function PluginApp() {
  const { t } = useTranslation('monitoring')
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs)
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState<string | null>(null)

  const inFlight = useRef(false)

  const setPref = useCallback(<K extends keyof Prefs>(key: K, value: Prefs[K]) => {
    persistPref(key, value)
    setPrefs((prev) => ({ ...prev, [key]: value }))
  }, [])

  // Only group_by / show_paused / show_unknown affect the server query.
  const queryKey = `${prefs.group_by}|${prefs.show_paused}|${prefs.show_unknown}`

  const fetchOverview = useCallback(async () => {
    if (inFlight.current) return
    if (typeof document !== 'undefined' && document.hidden) return
    inFlight.current = true
    try {
      const params = new URLSearchParams({
        group_by: prefs.group_by,
        hours: '24',
        include_paused: String(prefs.show_paused),
        include_unknown: String(prefs.show_unknown),
      })
      const result = await pluginApi.get<Overview>(`overview?${params.toString()}`)
      setData(result)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('app.load_error', { defaultValue: 'Failed to load' }))
    } finally {
      inFlight.current = false
    }
  }, [prefs.group_by, prefs.show_paused, prefs.show_unknown, t])

  // Refetch when the query-affecting prefs change; poll every 20s.
  useEffect(() => {
    void fetchOverview()
    const timer = setInterval(() => void fetchOverview(), 20_000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryKey])

  return (
    <div style={{ width: '100%', padding: '1.5rem 1rem 3rem' }}>
      <style>{`@media (max-width: 1023px){ .lx-mon-split{ grid-template-columns: 1fr !important; } }`}</style>

      {/* Centered control column */}
      <div style={{ maxWidth: 1280, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 16 }}>
        <HeaderCard stats={data?.stats ?? null} />
        <Toolbar prefs={prefs} setPref={setPref} onRefresh={() => void fetchOverview()} />
        {error && (
          <div
            style={{
              padding: '0.75rem 1rem',
              borderRadius: 'var(--lx-radius-md)',
              background: `color-mix(in srgb, ${STATE_COLOR.DOWN} 10%, transparent)`,
              border: `1px solid color-mix(in srgb, ${STATE_COLOR.DOWN} 25%, transparent)`,
              color: STATE_COLOR.DOWN,
              fontSize: '0.8rem',
            }}
          >
            {error}
          </div>
        )}
      </div>

      {/* Full-bleed data area */}
      <div style={{ width: '100%', marginTop: 16 }}>
        {!data && !error ? (
          <div style={{ textAlign: 'center', padding: '4rem 0', color: 'var(--lx-text-muted)', fontSize: '0.9rem' }}>
            {t('app.loading', { defaultValue: 'Loading monitors…' })}
          </div>
        ) : data ? (
          prefs.view_mode === 'table' ? (
            <TableView rows={data.rows} prefs={prefs} />
          ) : prefs.view_mode === 'split' ? (
            <SplitView rows={data.rows} groups={data.groups} prefs={prefs} />
          ) : (
            <CardsView groups={data.groups} prefs={prefs} />
          )
        ) : null}
      </div>
    </div>
  )
}
