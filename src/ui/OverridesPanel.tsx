import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, maintenanceApi, MaintenanceOverride } from './lib/api'

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** Active maintenance overrides (ours and those published by other plugins,
 *  e.g. a running deploy). Only overrides created here can be ended here. */
export default function OverridesPanel({ refreshKey, onChanged }: { refreshKey: number; onChanged: () => void }) {
  const { t } = useTranslation('monitoring')
  const [items, setItems] = useState<MaintenanceOverride[]>([])
  const [error, setError] = useState<string | null>(null)
  const [ending, setEnding] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const res = await maintenanceApi.listOverrides(true)
      setItems(res.items)
      setError(null)
    } catch (err) {
      // A 503 warm-up or a transient error must not hide the dashboard.
      setError(err instanceof ApiError ? err.message : null)
    }
  }, [])

  useEffect(() => {
    void load()
    const timer = setInterval(() => {
      if (typeof document === 'undefined' || !document.hidden) void load()
    }, 30_000)
    return () => clearInterval(timer)
  }, [load, refreshKey])

  if (items.length === 0 && !error) return null

  const end = async (id: string) => {
    setEnding(id)
    try {
      await maintenanceApi.endOverride(id)
      await load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setEnding(null)
    }
  }

  const color = 'var(--lx-state-maintenance, var(--lx-state-paused))'

  return (
    <div
      className="lx-card"
      style={{
        borderRadius: 'var(--lx-radius-md)',
        padding: '12px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        borderColor: `color-mix(in srgb, ${color} 35%, transparent)`,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
        <span style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.12em', fontWeight: 700, color }}>
          {t('overrides.title', { defaultValue: 'Active maintenance overrides' })}
        </span>
        <span style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>({items.length})</span>
      </div>
      {error && <div style={{ fontSize: '0.75rem', color: 'var(--lx-state-down)' }}>{error}</div>}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {items.map((o) => (
          <div
            key={o.override_id}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 12,
              flexWrap: 'wrap',
              fontSize: '0.8rem',
              padding: '6px 0',
              borderTop: '1px solid var(--lx-border-soft)',
            }}
          >
            <span style={{ fontWeight: 700, color: 'var(--lx-text)', minWidth: 0, flex: 1 }}>
              {o.reason || t('overrides.no_reason', { defaultValue: '(no reason)' })}
            </span>
            <span style={{ color: 'var(--lx-text-muted)' }}>
              {t('overrides.source', { defaultValue: 'by {{source}}', source: o.source })}
            </span>
            <span style={{ color: 'var(--lx-text-muted)' }} title={(o.ci_names ?? o.ci_ids).join(', ')}>
              {t('overrides.ci_count', { defaultValue: '{{count}} CIs', count: o.ci_ids.length })}
              {o.monitor_ids.length > 0 && ` · ${t('overrides.monitor_count', { defaultValue: '{{count}} monitors', count: o.monitor_ids.length })}`}
            </span>
            <span style={{ color: 'var(--lx-text-muted)', fontFamily: 'var(--lx-font-mono)' }}>
              {o.ended_at
                ? t('overrides.lingering_until', { defaultValue: 'ending, lingers until {{time}}', time: fmtTime(o.until_effective) })
                : t('overrides.until', { defaultValue: 'until {{time}}', time: fmtTime(o.until_effective) })}
            </span>
            {o.own && !o.ended_at && (
              <button
                type="button"
                className="lx-btn lx-btn--secondary lx-btn--sm"
                disabled={ending === o.override_id}
                onClick={() => void end(o.override_id)}
              >
                {t('overrides.end', { defaultValue: 'End' })}
              </button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
