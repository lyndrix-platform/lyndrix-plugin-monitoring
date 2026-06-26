import { useState, useEffect, useCallback } from 'react'
import { pluginApi } from './lib/api'

// ─── Types ────────────────────────────────────────────────────────────────────

type MonitorState = 'UP' | 'DOWN' | 'PAUSED' | 'UNKNOWN'

interface Monitor {
  monitor_id: string
  name: string
  monitor_type: string
  logical_group: string
  enabled: boolean
  latest_state: MonitorState
  latest_latency_ms: number | null
  latest_error: string | null
  last_checked_at: string | null
  is_admin_overridden: boolean
  uptime_24h: number
  uptime_7d: number
  uptime_30d: number
}

interface Stats {
  monitor_count: number
  up_count: number
  down_count: number
  paused_count: number
  uptime_all: number
}

interface DashboardData {
  monitors: Monitor[]
  stats: Stats
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const STATE_COLOR: Record<MonitorState, string> = {
  UP: 'var(--lx-state-up)',
  DOWN: 'var(--lx-state-down)',
  PAUSED: 'var(--lx-state-paused)',
  UNKNOWN: 'var(--lx-state-unknown)',
}

const STATE_LABEL: Record<MonitorState, string> = {
  UP: 'Online',
  DOWN: 'Offline',
  PAUSED: 'Pausiert',
  UNKNOWN: 'Unbekannt',
}

function formatLatency(ms: number | null): string {
  if (ms === null) return '—'
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}

function formatUptime(pct: number): string {
  return `${pct.toFixed(1)} %`
}

function timeAgo(iso: string | null): string {
  if (!iso) return '—'
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return `vor ${Math.round(diff)} s`
  if (diff < 3600) return `vor ${Math.round(diff / 60)} min`
  if (diff < 86400) return `vor ${Math.round(diff / 3600)} h`
  return `vor ${Math.round(diff / 86400)} Tagen`
}

function groupBy<T>(items: T[], key: (item: T) => string): Record<string, T[]> {
  return items.reduce<Record<string, T[]>>((acc, item) => {
    const k = key(item)
    ;(acc[k] ??= []).push(item)
    return acc
  }, {})
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function StatChip({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={{
      background: 'var(--lx-surface)',
      border: '1px solid var(--lx-border-soft)',
      borderRadius: 'var(--lx-radius-md)',
      padding: '0.75rem 1.25rem',
      minWidth: 90,
    }}>
      <div style={{ fontSize: '1.25rem', fontWeight: 700, color: color ?? 'var(--lx-text)' }}>
        {value}
      </div>
      <div style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)', marginTop: 2 }}>
        {label}
      </div>
    </div>
  )
}

function StateBadge({ state }: { state: MonitorState }) {
  const color = STATE_COLOR[state]
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 5,
      fontSize: '0.7rem',
      fontWeight: 600,
      color,
      background: `color-mix(in srgb, ${color} 12%, transparent)`,
      border: `1px solid color-mix(in srgb, ${color} 30%, transparent)`,
      borderRadius: 'var(--lx-radius-sm)',
      padding: '2px 7px',
      letterSpacing: '0.04em',
      textTransform: 'uppercase',
    }}>
      <span style={{
        width: 6, height: 6,
        borderRadius: '50%',
        background: color,
        flexShrink: 0,
      }} />
      {STATE_LABEL[state]}
    </span>
  )
}

function MonitorCard({ monitor }: { monitor: Monitor }) {
  return (
    <div style={{
      background: 'var(--lx-surface)',
      border: '1px solid var(--lx-border-soft)',
      borderRadius: 'var(--lx-radius-md)',
      padding: '0.875rem 1rem',
      display: 'flex',
      alignItems: 'center',
      gap: '0.75rem',
    }}>
      {/* State dot (left accent) */}
      <div style={{
        width: 4,
        alignSelf: 'stretch',
        borderRadius: 2,
        background: STATE_COLOR[monitor.latest_state],
        flexShrink: 0,
      }} />

      {/* Name + error */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: '0.875rem',
          fontWeight: 600,
          color: 'var(--lx-text)',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>
          {monitor.name}
          {monitor.is_admin_overridden && (
            <span style={{ marginLeft: 6, fontSize: '0.65rem', color: 'var(--lx-accent-3)' }}>
              ▲ manuell
            </span>
          )}
        </div>
        {monitor.latest_error && (
          <div style={{
            fontSize: '0.7rem',
            color: STATE_COLOR.DOWN,
            marginTop: 2,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}>
            {monitor.latest_error}
          </div>
        )}
      </div>

      {/* Meta */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', flexShrink: 0 }}>
        <div style={{ textAlign: 'right', fontSize: '0.7rem', color: 'var(--lx-text-muted)' }}>
          <div>{formatLatency(monitor.latest_latency_ms)}</div>
          <div>{timeAgo(monitor.last_checked_at)}</div>
        </div>
        <div style={{ textAlign: 'right', fontSize: '0.7rem', color: 'var(--lx-text-muted)' }}>
          <div title="Uptime 24h">{formatUptime(monitor.uptime_24h)}</div>
          <div style={{ opacity: 0.6 }}>24h</div>
        </div>
        <StateBadge state={monitor.latest_state} />
      </div>
    </div>
  )
}

function GroupSection({ name, monitors }: { name: string; monitors: Monitor[] }) {
  const downCount = monitors.filter((m) => m.latest_state === 'DOWN').length
  return (
    <section style={{ marginBottom: '1.5rem' }}>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '0.5rem',
        marginBottom: '0.5rem',
      }}>
        <h2 style={{
          margin: 0,
          fontSize: '0.8rem',
          fontWeight: 600,
          color: 'var(--lx-text-muted)',
          textTransform: 'uppercase',
          letterSpacing: '0.08em',
        }}>
          {name}
        </h2>
        {downCount > 0 && (
          <span style={{
            fontSize: '0.65rem',
            fontWeight: 700,
            color: STATE_COLOR.DOWN,
            background: `color-mix(in srgb, ${STATE_COLOR.DOWN} 12%, transparent)`,
            border: `1px solid color-mix(in srgb, ${STATE_COLOR.DOWN} 25%, transparent)`,
            borderRadius: 'var(--lx-radius-sm)',
            padding: '1px 6px',
          }}>
            {downCount} offline
          </span>
        )}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
        {monitors.map((m) => <MonitorCard key={m.monitor_id} monitor={m} />)}
      </div>
    </section>
  )
}

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function PluginApp() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)

  const fetchDashboard = useCallback(async () => {
    try {
      const result = await pluginApi.get<DashboardData>('dashboard')
      setData(result)
      setLastUpdated(new Date())
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Fehler beim Laden')
    }
  }, [])

  useEffect(() => {
    void fetchDashboard()
    const timer = setInterval(() => void fetchDashboard(), 30_000)
    return () => clearInterval(timer)
  }, [fetchDashboard])

  const groups = data ? groupBy(data.monitors, (m) => m.logical_group) : {}

  return (
    <div style={{ maxWidth: 900, margin: '0 auto', padding: '1.5rem 1.5rem 3rem' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.25rem' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: '1.125rem', fontWeight: 700, color: 'var(--lx-text)' }}>
            State Monitoring
          </h1>
          {lastUpdated && (
            <p style={{ margin: '2px 0 0', fontSize: '0.7rem', color: 'var(--lx-text-muted)' }}>
              Aktualisiert: {lastUpdated.toLocaleTimeString('de-DE')}
            </p>
          )}
        </div>
        <button
          onClick={() => void fetchDashboard()}
          style={{
            padding: '0.375rem 0.75rem',
            borderRadius: 'var(--lx-radius-sm)',
            border: '1px solid var(--lx-border-soft)',
            background: 'var(--lx-surface)',
            color: 'var(--lx-text)',
            fontSize: '0.75rem',
            cursor: 'pointer',
          }}
        >
          Aktualisieren
        </button>
      </div>

      {/* Error */}
      {error && (
        <div style={{
          padding: '0.75rem 1rem',
          borderRadius: 'var(--lx-radius-md)',
          background: `color-mix(in srgb, ${STATE_COLOR.DOWN} 10%, transparent)`,
          border: `1px solid color-mix(in srgb, ${STATE_COLOR.DOWN} 25%, transparent)`,
          color: STATE_COLOR.DOWN,
          fontSize: '0.8rem',
          marginBottom: '1rem',
        }}>
          {error}
        </div>
      )}

      {/* Stats */}
      {data && (
        <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', marginBottom: '1.5rem' }}>
          <StatChip label="Gesamt" value={data.stats.monitor_count} />
          <StatChip label="Online" value={data.stats.up_count} color={STATE_COLOR.UP} />
          <StatChip label="Offline" value={data.stats.down_count} color={STATE_COLOR.DOWN} />
          <StatChip label="Pausiert" value={data.stats.paused_count} color={STATE_COLOR.PAUSED} />
          <StatChip label="Uptime ø" value={`${data.stats.uptime_all.toFixed(1)} %`} color="var(--lx-accent)" />
        </div>
      )}

      {/* Loading skeleton */}
      {!data && !error && (
        <div style={{ color: 'var(--lx-text-muted)', fontSize: '0.875rem', textAlign: 'center', padding: '3rem 0' }}>
          Lade Monitore…
        </div>
      )}

      {/* Monitor groups */}
      {Object.entries(groups)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([group, monitors]) => (
          <GroupSection key={group} name={group} monitors={monitors} />
        ))}

      {data && data.monitors.length === 0 && (
        <div style={{ color: 'var(--lx-text-muted)', fontSize: '0.875rem', textAlign: 'center', padding: '3rem 0' }}>
          Keine Monitore konfiguriert.
        </div>
      )}
    </div>
  )
}
