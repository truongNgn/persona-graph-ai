import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config/backend'
import { useChatStore } from '../store/chatStore'
import './AdminDashboard.css'

type Metrics = Record<string, string | number | null>
interface Generation {
  id: string; traceId: string; name: string; model: string | null; startTime: string; endTime: string | null
  level: string; statusMessage: string | null; latency: number | null
  timeToFirstToken: number | null; totalCost: number | null
  usageDetails: Record<string, number> | null; userId: string | null; sessionId: string | null
  input?: unknown; output?: unknown; metadata?: unknown
}
interface Dashboard {
  status: 'unconfigured' | 'connected'; tracing_enabled: boolean; environment: string
  from_time: string; to_time: string; summary: Metrics; models: Metrics[]; trend: Metrics[]
  generations: Generation[]; cursor: string | null
}

const count = (value: unknown) => Number(value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })
const money = (value: unknown) => value == null ? '—' : Number(value).toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 5 })
const duration = (value: unknown, milliseconds = false) => value == null ? '—' : `${(Number(value) / (milliseconds ? 1000 : 1)).toFixed(2)} s`
const pretty = (value: unknown): string => {
  if (value == null) return 'Not recorded'
  if (typeof value === 'string') { try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value } }
  return JSON.stringify(value, null, 2)
}

async function read<T>(path: string, token: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE_URL}/api/admin/monitoring${path}`, {
    headers: { Authorization: `Bearer ${token}` }, signal, cache: 'no-store',
  })
  if (response.status === 401) useChatStore.getState().logout()
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail || `Request failed (${response.status}).`)
  }
  return response.json() as Promise<T>
}

export function AdminDashboard() {
  const token = useChatStore(state => state.authToken)
  const user = useChatStore(state => state.authUser)
  const [access, setAccess] = useState<'checking' | 'allowed' | 'denied' | 'error'>('checking')
  const [data, setData] = useState<Dashboard | null>(null)
  const [days, setDays] = useState(7)
  const [model, setModel] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [refresh, setRefresh] = useState(0)
  const [auto, setAuto] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [errorsOnly, setErrorsOnly] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<Generation | null>(null)
  const [detailError, setDetailError] = useState('')
  const [paging, setPaging] = useState(false)
  const [updated, setUpdated] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const pageAbort = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    setAccess('checking')
    read<{ allowed: boolean }>('/access', token, controller.signal)
      .then(result => setAccess(result.allowed ? 'allowed' : 'denied'))
      .catch(err => { if (!controller.signal.aborted) { setError(String(err.message)); setAccess('error') } })
    return () => controller.abort()
  }, [token])

  useEffect(() => {
    if (access !== 'allowed') return
    const controller = new AbortController()
    pageAbort.current?.abort()
    setPaging(false); setLoading(true); setError(''); setData(null)
    const query = new URLSearchParams({ days: String(days) })
    if (model) query.set('model', model)
    read<Dashboard>(`/dashboard?${query}`, token, controller.signal).then(result => {
      setData(result)
      setModels(previous => Array.from(new Set([...previous, ...result.models.map(row => String(row.providedModelName || '')).filter(Boolean)])))
      setUpdated(new Date().toLocaleTimeString())
    }).catch(err => { if (!controller.signal.aborted) setError(String(err.message)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => { controller.abort(); pageAbort.current?.abort() }
  }, [access, token, days, model, refresh])

  useEffect(() => {
    if (!auto) return
    const timer = window.setInterval(() => { if (!document.hidden && !loading && !paging && !selected) setRefresh(value => value + 1) }, 60000)
    return () => window.clearInterval(timer)
  }, [auto, loading, paging, selected])

  useEffect(() => {
    if (!selected) return
    const controller = new AbortController()
    setDetail(null); setDetailError('')
    dialog.current?.showModal()
    read<Generation>(`/generations/${encodeURIComponent(selected)}`, token, controller.signal)
      .then(setDetail).catch(err => { if (!controller.signal.aborted) setDetailError(String(err.message)) })
    return () => controller.abort()
  }, [selected, token])

  async function loadMore() {
    if (!data?.cursor || paging) return
    const controller = new AbortController()
    pageAbort.current = controller
    setPaging(true); setError('')
    const query = new URLSearchParams({ days: String(days), end_at: data.to_time, cursor: data.cursor })
    if (model) query.set('model', model)
    try {
      const result = await read<{ generations: Generation[]; cursor: string | null }>(`/generations?${query}`, token, controller.signal)
      setData(previous => previous && ({ ...previous, cursor: result.cursor, generations: Array.from(new Map([...previous.generations, ...result.generations].map(row => [row.id, row])).values()) }))
    } catch (err) { if (!controller.signal.aborted) setError((err as Error).message) }
    finally { if (!controller.signal.aborted) setPaging(false) }
  }

  const summary = data?.summary || {}
  const requests = Number(summary.count_count || 0)
  const failed = (data?.models || []).filter(row => row.level === 'ERROR').reduce((sum, row) => sum + Number(row.count_count || 0), 0)
  const grouped = new Map<string, { requests: number; cost: number; errors: number; latency: number }>()
  for (const row of data?.models || []) {
    const name = String(row.providedModelName || 'Unknown model')
    const value = grouped.get(name) || { requests: 0, cost: 0, errors: 0, latency: 0 }
    const n = Number(row.count_count || 0)
    value.requests += n; value.cost += Number(row.sum_totalCost || 0)
    value.errors += row.level === 'ERROR' ? n : 0
    value.latency += Number(row.avg_latency || 0) * n
    grouped.set(name, value)
  }
  const trend = [...(data?.trend || [])].sort((a, b) => String(a.time_dimension).localeCompare(String(b.time_dimension)))
  const maxCount = Math.max(1, ...trend.map(row => Number(row.count_count || 0)))
  const generations = (data?.generations || []).filter(row =>
    (!errorsOnly || row.level === 'ERROR') && `${row.name} ${row.model} ${row.traceId} ${row.userId} ${row.sessionId}`.toLowerCase().includes(search.toLowerCase()))

  return <div className="monitor">
    <aside className="monitor-nav">
      <a className="monitor-brand" href="/">P<span>Persona Graph</span></a>
      <div className="monitor-nav-label">ADMIN WORKSPACE</div>
      <a className="active" href="#overview">◈ &nbsp; Model monitoring</a>
      <a href="#models">▤ &nbsp; Model comparison</a>
      <a href="#generations">≡ &nbsp; Generations</a>
      <div className="monitor-nav-bottom"><span>{user?.display_name || user?.email}</span><a href="/">← Back to chat</a></div>
    </aside>
    <main className="monitor-main" id="overview">
      <header className="monitor-header"><div><div className="monitor-eyebrow">OBSERVABILITY / LANGFUSE</div><h1>Model monitoring</h1><p>A clear view of your AI models, from first token to final response.</p></div>
        <span className={`monitor-status ${data?.status === 'connected' ? 'connected' : ''}`}><i />{data?.status === 'connected' ? 'Connected' : data?.status === 'unconfigured' ? 'Setup required' : 'Admin dashboard'}</span>
      </header>
      {access === 'checking' && <div className="monitor-empty" role="status">Checking administrator access…</div>}
      {(access === 'denied' || access === 'error') && <div className="monitor-empty"><h2>{access === 'error' ? 'Monitoring unavailable' : 'Administrator access required'}</h2><p>{error || 'Your account does not have permission to view project monitoring data.'}</p>{access === 'error' && <button onClick={() => window.location.reload()}>Retry</button>}<p><a href="/">Return to chat</a></p></div>}
      {access === 'allowed' && <>
        <div className="monitor-toolbar">
          <label>Time range<select value={days} onChange={event => setDays(Number(event.target.value))}><option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option></select></label>
          <label>Model<select value={model} onChange={event => setModel(event.target.value)}><option value="">All models</option>{models.map(name => <option key={name}>{name}</option>)}</select></label>
          <label className="monitor-auto"><input type="checkbox" checked={auto} onChange={event => setAuto(event.target.checked)} /> Refresh every minute</label>
          <button onClick={() => setRefresh(value => value + 1)} disabled={loading || paging}>{loading ? 'Refreshing…' : '↻ Refresh'}</button>
        </div>
        {error && <div className="monitor-alert" role="alert">{error}</div>}
        {loading && <div className="monitor-empty" role="status">Loading monitoring data…</div>}
        {data?.status === 'unconfigured' && <div className="monitor-empty"><div className="monitor-empty-icon">◈</div><h2>Connect your Langfuse project</h2><p>Add your Langfuse project keys to the backend configuration, then restart the backend. Your model activity will appear here after the first traced chat.</p><p>No monitoring data has been loaded.</p></div>}
        {data?.status === 'connected' && <>
          {!data.tracing_enabled && <div className="monitor-notice">Reading stored data. New chat tracing is currently disabled.</div>}
          <div className="monitor-context"><span>Environment <strong>{data.environment}</strong> · Generation attempts, including fallbacks</span><span>Updated {updated}</span></div>
          <section className="monitor-kpis" aria-label="Key metrics">
            {[
              ['Generations', count(requests), 'Model calls in this period'],
              ['Total cost', money(summary.sum_totalCost), 'Reported model cost · USD'],
              ['Tokens', count(Number(summary.sum_inputTokens || 0) + Number(summary.sum_outputTokens || 0)), `${count(summary.sum_inputTokens)} input / ${count(summary.sum_outputTokens)} output`],
              ['Average latency', duration(summary.avg_latency, true), 'Full generation duration'],
              ['First token', duration(summary.avg_timeToFirstToken, true), 'Average time to first token'],
              ['Failed attempts', count(failed), requests ? `${(failed / requests * 100).toFixed(1)}% of model calls` : 'No model calls recorded'],
            ].map(([label, value, caption]) => <article className="monitor-card" key={label}><span>{label}</span><strong>{value}</strong><small>{caption}</small></article>)}
          </section>
          <section className="monitor-panel"><div className="monitor-section-title"><div><h2>Daily activity</h2><p>Generation attempts per day · UTC</p></div><span className="monitor-legend"><i /> Generations</span></div>
            {trend.length ? <div className="monitor-chart" aria-label="Daily generation counts">{trend.map(row => <div className="monitor-chart-column" key={String(row.time_dimension)}><span>{count(row.count_count)}</span><div className="monitor-chart-track"><div className="monitor-chart-bar" style={{ height: `${Number(row.count_count || 0) / maxCount * 100}%` }} title={`${row.time_dimension}: ${count(row.count_count)} generations, ${money(row.sum_totalCost)}`} /></div><small>{String(row.time_dimension).slice(5, 10)}</small></div>)}</div> : <div className="monitor-inline-empty">No generation activity in this period. Send a traced chat to get started.</div>}
          </section>
          <section className="monitor-panel" id="models"><div className="monitor-section-title"><div><h2>Model comparison</h2><p>Compare usage and performance across your models.</p></div><span>{grouped.size} models</span></div>
            <div className="monitor-table-wrap"><table><thead><tr><th>Model</th><th>Generations</th><th>Cost</th><th>Avg. latency</th><th>Failed attempts</th></tr></thead><tbody>{Array.from(grouped).sort((a, b) => b[1].requests - a[1].requests).map(([name, value]) => <tr key={name}><td className="monitor-model-name">{name}</td><td>{count(value.requests)}</td><td>{money(value.cost)}</td><td>{duration(value.requests ? value.latency / value.requests : null, true)}</td><td><span className={value.errors ? 'monitor-error-text' : ''}>{count(value.errors)}</span></td></tr>)}</tbody></table></div>
            {!grouped.size && <div className="monitor-inline-empty">No models recorded in this period.</div>}
          </section>
          <section className="monitor-panel" id="generations"><div className="monitor-section-title"><div><h2>Generations</h2><p>Inspect a model call to see its prompt, response, and diagnostics.</p></div><span>{data.generations.length} loaded</span></div>
            <div className="monitor-table-tools"><input aria-label="Search loaded generations" placeholder="Search loaded calls by model, user, session, or trace…" value={search} onChange={event => setSearch(event.target.value)} /><label><input type="checkbox" checked={errorsOnly} onChange={event => setErrorsOnly(event.target.checked)} /> Errors only (loaded calls)</label></div>
            <div className="monitor-table-wrap"><table><thead><tr><th>Started</th><th>Model / call</th><th>Status</th><th>Latency</th><th>Tokens</th><th>Cost</th><th>Details</th></tr></thead><tbody>{[...generations].sort((a, b) => b.startTime.localeCompare(a.startTime)).map(row => <tr key={row.id}><td>{new Date(row.startTime).toLocaleString()}</td><td className="monitor-model-name">{row.model || row.name}<small>{row.userId || 'Unknown user'}</small></td><td><span className={`monitor-badge ${row.level === 'ERROR' ? 'failed' : ''}`}>{row.level === 'ERROR' ? 'Error' : row.level === 'WARNING' ? 'Warning' : row.endTime === null ? 'Running' : 'Recorded'}</span></td><td>{duration(row.latency)}</td><td>{row.usageDetails ? count(row.usageDetails.total ?? ((row.usageDetails.input || 0) + (row.usageDetails.output || 0))) : '—'}</td><td>{money(row.totalCost)}</td><td><button className="monitor-detail-button" onClick={() => setSelected(row.id)}>Inspect ↗</button></td></tr>)}</tbody></table></div>
            {!generations.length && <div className="monitor-inline-empty">{data.generations.length ? 'No loaded calls match these filters.' : 'No generations recorded in this period.'}</div>}
            {data.cursor && <div className="monitor-pagination"><button disabled={paging || loading} onClick={() => void loadMore()}>{paging ? 'Loading…' : 'Load more generations'}</button></div>}
          </section>
          <p className="monitor-footnote">Totals come from Langfuse across the selected period. Missing usage or pricing may leave costs incomplete. Search and error filters apply to loaded calls.</p>
        </>}
      </>}
    </main>
    {selected && <dialog className="monitor-dialog" ref={dialog} onClose={() => setSelected(null)}><div className="monitor-dialog-heading"><div><div className="monitor-eyebrow">GENERATION DETAILS</div><h2>{detail?.model || 'Model call'}</h2></div><button aria-label="Close generation details" onClick={() => dialog.current?.close()}>✕</button></div>
      {detailError ? <div role="alert" className="monitor-alert">{detailError}</div> : !detail ? <p role="status">Loading details…</p> : <>
        <dl className="monitor-detail-meta">{[['Trace', detail.traceId], ['Session', detail.sessionId], ['User', detail.userId], ['Latency', duration(detail.latency)], ['First token', duration(detail.timeToFirstToken)], ['Status', detail.level], ['Cost', money(detail.totalCost)]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || '—'}</dd></div>)}</dl>
        {detail.statusMessage && <div className="monitor-alert">{detail.statusMessage}</div>}
        {[['Prompt / input', detail.input], ['Response / output', detail.output], ['Usage', detail.usageDetails], ['Metadata', detail.metadata]].map(([label, value]) => <section key={String(label)}><h3>{String(label)}</h3><pre>{pretty(value)}</pre></section>)}
      </>}
    </dialog>}
  </div>
}
