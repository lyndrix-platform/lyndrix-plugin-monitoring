import { useEffect, useMemo, useState, CSSProperties } from 'react'
import { useTranslation } from 'react-i18next'
import { ApiError, maintenanceApi } from './lib/api'

/** What the dialog acts on: a host card (host monitor + its services) or a
 *  selection the operator makes in the picker (several hosts). */
export interface MaintenanceTarget {
  label: string
  monitor_ids: string[]
  ci_ids: string[]
}

export interface HostOption {
  key: string
  label: string
  monitor_ids: string[]
  ci_ids: string[]
}

type Mode = 'override' | 'window'
type WindowKind = 'weekly' | 'once'

const DAYS: { key: string; i18n: string; fallback: string }[] = [
  { key: 'mon', i18n: 'maintenance.day_mon', fallback: 'Mo' },
  { key: 'tue', i18n: 'maintenance.day_tue', fallback: 'Tu' },
  { key: 'wed', i18n: 'maintenance.day_wed', fallback: 'We' },
  { key: 'thu', i18n: 'maintenance.day_thu', fallback: 'Th' },
  { key: 'fri', i18n: 'maintenance.day_fri', fallback: 'Fr' },
  { key: 'sat', i18n: 'maintenance.day_sat', fallback: 'Sa' },
  { key: 'sun', i18n: 'maintenance.day_sun', fallback: 'Su' },
]

const OVERLAY: CSSProperties = {
  position: 'fixed',
  inset: 0,
  zIndex: 1100,
  background: 'rgba(0,0,0,0.45)',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: 16,
}

const PANEL: CSSProperties = {
  width: 'min(560px, 100%)',
  maxHeight: 'calc(100vh - 32px)',
  overflowY: 'auto',
  borderRadius: 'var(--lx-radius-lg, var(--lx-radius-md))',
  background: 'var(--lx-elevated, var(--lx-surface))',
  border: '1px solid var(--lx-glass-border, var(--lx-border-soft))',
  boxShadow: 'var(--lx-glow, 0 8px 30px rgba(0,0,0,0.35))',
  padding: 20,
  display: 'flex',
  flexDirection: 'column',
  gap: 14,
  color: 'var(--lx-text)',
}

const LABEL: CSSProperties = {
  fontSize: '0.62rem',
  textTransform: 'uppercase',
  letterSpacing: '0.1em',
  color: 'var(--lx-text-muted)',
  fontWeight: 700,
}

const FIELD: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 4 }

function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/** ``datetime-local`` value (local wall time) → ISO-8601 with offset. */
function localToIso(value: string): string {
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? '' : d.toISOString()
}

export default function MaintenanceDialog({
  open,
  target,
  hosts,
  onClose,
  onDone,
}: {
  open: boolean
  target: MaintenanceTarget | null
  hosts: HostOption[]
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useTranslation('monitoring')
  const [mode, setMode] = useState<Mode>('override')
  const [picked, setPicked] = useState<string[]>([])
  const [defaults, setDefaults] = useState<{ default_minutes: number; max_minutes: number }>({
    default_minutes: 60,
    max_minutes: 240,
  })
  const [minutes, setMinutes] = useState<number>(60)
  const [reason, setReason] = useState('')
  const [kind, setKind] = useState<WindowKind>('weekly')
  const [days, setDays] = useState<string[]>(['sun'])
  const [start, setStart] = useState('02:00')
  const [end, setEnd] = useState('04:00')
  const [tz, setTz] = useState(localTimeZone())
  const [onceStart, setOnceStart] = useState('')
  const [onceEnd, setOnceEnd] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Defaults come from the plugin settings (override_default/max_minutes).
  useEffect(() => {
    if (!open) return
    setError(null)
    setBusy(false)
    void maintenanceApi
      .listOverrides(true)
      .then((res) => {
        setDefaults(res.defaults)
        setMinutes(res.defaults.default_minutes)
      })
      .catch(() => undefined)
  }, [open])

  const effectiveTarget = useMemo<MaintenanceTarget | null>(() => {
    if (target) return target
    const chosen = hosts.filter((h) => picked.includes(h.key))
    if (chosen.length === 0) return null
    return {
      label: chosen.map((h) => h.label).join(', '),
      monitor_ids: Array.from(new Set(chosen.flatMap((h) => h.monitor_ids))),
      ci_ids: Array.from(new Set(chosen.flatMap((h) => h.ci_ids))),
    }
  }, [target, hosts, picked])

  if (!open) return null

  const presets = [30, 60, 120, 240].filter((m) => m <= defaults.max_minutes)

  const submit = async () => {
    if (!effectiveTarget) {
      setError(t('maintenance.pick_hosts', { defaultValue: 'Select at least one host.' }))
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (mode === 'override') {
        await maintenanceApi.createOverride({
          monitor_ids: effectiveTarget.monitor_ids,
          ci_ids: effectiveTarget.ci_ids,
          minutes: Math.max(1, Math.min(minutes, defaults.max_minutes)),
          reason,
        })
      } else {
        const window =
          kind === 'weekly'
            ? { weekly: { days, start, end, tz } }
            : { once: { start: localToIso(onceStart), end: localToIso(onceEnd), note: note || undefined } }
        // One manual row per monitor — the backend resolves the CI and merges.
        for (const monitorId of effectiveTarget.monitor_ids) {
          await maintenanceApi.createWindow({ monitor_id: monitorId, windows: [window], note: note || undefined })
        }
      }
      onDone()
      onClose()
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const tab = (value: Mode, label: string) => {
    const active = mode === value
    return (
      <button
        key={value}
        type="button"
        onClick={() => setMode(value)}
        className={`lx-btn lx-btn--sm ${active ? 'lx-btn--primary' : 'lx-btn--ghost'}`}
      >
        {label}
      </button>
    )
  }

  return (
    <div style={OVERLAY} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div style={PANEL} role="dialog" aria-modal="true">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <div>
            <div style={{ fontSize: '1.05rem', fontWeight: 800 }}>{t('maintenance.title', { defaultValue: 'Maintenance' })}</div>
            <div style={{ fontSize: '0.78rem', color: 'var(--lx-text-muted)', marginTop: 2 }}>
              {effectiveTarget
                ? t('maintenance.target', {
                    defaultValue: '{{label}} · {{count}} monitors',
                    label: effectiveTarget.label,
                    count: effectiveTarget.monitor_ids.length,
                  })
                : t('maintenance.no_target', { defaultValue: 'Choose the hosts to put into maintenance.' })}
            </div>
          </div>
          <button type="button" className="lx-btn lx-btn--ghost lx-btn--sm" onClick={onClose} aria-label="close">
            ✕
          </button>
        </div>

        {!target && (
          <label style={FIELD}>
            <span style={LABEL}>{t('maintenance.hosts', { defaultValue: 'Hosts' })}</span>
            <select
              multiple
              className="lx-select"
              size={Math.min(8, Math.max(3, hosts.length))}
              value={picked}
              onChange={(e) => setPicked(Array.from(e.target.selectedOptions).map((o) => o.value))}
            >
              {hosts.map((h) => (
                <option key={h.key} value={h.key}>
                  {h.label}
                </option>
              ))}
            </select>
          </label>
        )}

        <div style={{ display: 'flex', gap: 6 }}>
          {tab('override', t('maintenance.mode_override', { defaultValue: 'Override now' }))}
          {tab('window', t('maintenance.mode_window', { defaultValue: 'Recurring / planned window' }))}
        </div>

        {mode === 'override' ? (
          <>
            <div style={FIELD}>
              <span style={LABEL}>{t('maintenance.duration', { defaultValue: 'Duration' })}</span>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
                {presets.map((m) => (
                  <button
                    key={m}
                    type="button"
                    className={`lx-btn lx-btn--sm ${minutes === m ? 'lx-btn--primary' : 'lx-btn--secondary'}`}
                    onClick={() => setMinutes(m)}
                  >
                    {t('maintenance.minutes', { defaultValue: '{{count}} min', count: m })}
                  </button>
                ))}
                <input
                  type="number"
                  className="lx-input"
                  min={1}
                  max={defaults.max_minutes}
                  value={minutes}
                  onChange={(e) => setMinutes(Number(e.target.value) || 1)}
                  style={{ width: 90 }}
                />
                <span style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
                  {t('maintenance.max_minutes', { defaultValue: 'max. {{count}} min', count: defaults.max_minutes })}
                </span>
              </div>
            </div>
            <label style={FIELD}>
              <span style={LABEL}>{t('maintenance.reason', { defaultValue: 'Reason' })}</span>
              <input
                className="lx-input"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder={t('maintenance.reason_placeholder', { defaultValue: 'e.g. kernel update' })}
              />
            </label>
          </>
        ) : (
          <>
            <div style={{ display: 'flex', gap: 6 }}>
              {(['weekly', 'once'] as WindowKind[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  className={`lx-btn lx-btn--sm ${kind === k ? 'lx-btn--primary' : 'lx-btn--secondary'}`}
                  onClick={() => setKind(k)}
                >
                  {k === 'weekly'
                    ? t('maintenance.kind_weekly', { defaultValue: 'Weekly' })
                    : t('maintenance.kind_once', { defaultValue: 'Once' })}
                </button>
              ))}
            </div>
            {kind === 'weekly' ? (
              <>
                <div style={FIELD}>
                  <span style={LABEL}>{t('maintenance.days', { defaultValue: 'Days' })}</span>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {DAYS.map((d) => {
                      const on = days.includes(d.key)
                      return (
                        <button
                          key={d.key}
                          type="button"
                          className={`lx-btn lx-btn--sm ${on ? 'lx-btn--primary' : 'lx-btn--secondary'}`}
                          onClick={() => setDays(on ? days.filter((x) => x !== d.key) : [...days, d.key])}
                        >
                          {t(d.i18n, { defaultValue: d.fallback })}
                        </button>
                      )
                    })}
                  </div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1.6fr', gap: 10 }}>
                  <label style={FIELD}>
                    <span style={LABEL}>{t('maintenance.start', { defaultValue: 'Start' })}</span>
                    <input type="time" className="lx-input" value={start} onChange={(e) => setStart(e.target.value)} />
                  </label>
                  <label style={FIELD}>
                    <span style={LABEL}>{t('maintenance.end', { defaultValue: 'End' })}</span>
                    <input type="time" className="lx-input" value={end} onChange={(e) => setEnd(e.target.value)} />
                  </label>
                  <label style={FIELD}>
                    <span style={LABEL}>{t('maintenance.timezone', { defaultValue: 'Time zone' })}</span>
                    <input className="lx-input" value={tz} onChange={(e) => setTz(e.target.value)} />
                  </label>
                </div>
                <div style={{ fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
                  {t('maintenance.over_midnight', { defaultValue: 'An end before the start means "over midnight".' })}
                </div>
              </>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <label style={FIELD}>
                  <span style={LABEL}>{t('maintenance.start', { defaultValue: 'Start' })}</span>
                  <input
                    type="datetime-local"
                    className="lx-input"
                    value={onceStart}
                    onChange={(e) => setOnceStart(e.target.value)}
                  />
                </label>
                <label style={FIELD}>
                  <span style={LABEL}>{t('maintenance.end', { defaultValue: 'End' })}</span>
                  <input type="datetime-local" className="lx-input" value={onceEnd} onChange={(e) => setOnceEnd(e.target.value)} />
                </label>
              </div>
            )}
            <label style={FIELD}>
              <span style={LABEL}>{t('maintenance.note', { defaultValue: 'Note' })}</span>
              <input className="lx-input" value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
          </>
        )}

        {error && (
          <div
            style={{
              padding: '8px 12px',
              borderRadius: 'var(--lx-radius-md)',
              background: 'color-mix(in srgb, var(--lx-state-down) 10%, transparent)',
              border: '1px solid color-mix(in srgb, var(--lx-state-down) 25%, transparent)',
              color: 'var(--lx-state-down)',
              fontSize: '0.78rem',
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button type="button" className="lx-btn lx-btn--secondary" onClick={onClose} disabled={busy}>
            {t('maintenance.cancel', { defaultValue: 'Cancel' })}
          </button>
          <button type="button" className="lx-btn lx-btn--primary" onClick={() => void submit()} disabled={busy || !effectiveTarget}>
            {mode === 'override'
              ? t('maintenance.start_override', { defaultValue: 'Start override' })
              : t('maintenance.save_window', { defaultValue: 'Save window' })}
          </button>
        </div>
      </div>
    </div>
  )
}
