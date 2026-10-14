import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowRight, Check, Clock3, Grid2X2, List, Mic, Moon, Play, Plus, RotateCcw, Sparkles, Square, Sun, Timer, TrendingUp, Upload, Volume2, VolumeX } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import './App.css'

const API_BASE = '/api'

const initialForm = {
  role: '',
  experience_level: 'Mid-level',
  interview_type: 'Mixed',
  question_count: 5,
  resume_text: '',
  job_description: '',
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(data.detail || `Request failed (${response.status})`)
  }
  return data
}

function Icon({ name, size = 20 }) {
  const icons = {
    grid: Grid2X2,
    list: List,
    spark: Sparkles,
    arrow: ArrowRight,
    clock: Clock3,
    trend: TrendingUp,
    mic: Mic,
    volume: Volume2,
    upload: Upload,
    check: Check,
    plus: Plus,
    play: Play,
    retry: RotateCcw,
    stop: Square,
    timer: Timer,
    volumeMute: VolumeX,
    sun: Sun,
    moon: Moon,
  }
  const IconComponent = icons[name]
  return <IconComponent aria-hidden="true" size={size} strokeWidth={1.8} />
}

function formatDate(value) {
  return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric' }).format(new Date(value))
}

function formatToday() {
  return new Intl.DateTimeFormat('en', { weekday: 'long', month: 'long', day: 'numeric' })
    .format(new Date())
    .toUpperCase()
}

function formatDuration(seconds) {
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function App() {
  const location = useLocation()
  const routerNavigate = useNavigate()
  const routeSegments = location.pathname.split('/').filter(Boolean)
  const page = routeSegments[0] || 'dashboard'
  const routeInterviewId = ['session', 'results'].includes(page) ? routeSegments[1] : null
  const pageRef = useRef(page)
  const [theme, setTheme] = useState(() => window.localStorage.getItem('interviewai-theme') === 'dark' ? 'dark' : 'light')
  const [dashboard, setDashboard] = useState(null)
  const [interviews, setInterviews] = useState([])
  const [llmStatus, setLlmStatus] = useState(null)
  const [mcpStatus, setMcpStatus] = useState(null)
  const [interview, setInterview] = useState(null)
  const interviewRef = useRef(interview)
  const [form, setForm] = useState(initialForm)
  const [answer, setAnswer] = useState('')
  const [latestEvaluation, setLatestEvaluation] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState('')
  const [documentAnalysis, setDocumentAnalysis] = useState(null)
  const [analyzingDocuments, setAnalyzingDocuments] = useState(false)
  const [recording, setRecording] = useState(false)
  const [recordedAudio, setRecordedAudio] = useState(null)
  const [audioUrl, setAudioUrl] = useState('')
  const [recordingSeconds, setRecordingSeconds] = useState(0)
  const [transcribing, setTranscribing] = useState(false)
  const [microphoneError, setMicrophoneError] = useState('')
  const [speechMuted, setSpeechMuted] = useState(false)
  const [availableVoices, setAvailableVoices] = useState([])
  const [selectedVoiceURI, setSelectedVoiceURI] = useState('')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const mediaRecorderRef = useRef(null)
  const mediaStreamRef = useRef(null)
  const audioChunksRef = useRef([])
  const recordingStartedAtRef = useRef(0)
  const discardOnRecorderStopRef = useRef(false)

  useEffect(() => {
    window.localStorage.setItem('interviewai-theme', theme)
  }, [theme])

  useLayoutEffect(() => {
    pageRef.current = page
    interviewRef.current = interview
  }, [page, interview])

  useEffect(() => {
    const updateVoices = () => {
      const voices = window.speechSynthesis?.getVoices() || []
      const englishVoices = voices.filter((voice) => voice.lang.toLowerCase().startsWith('en'))
      setAvailableVoices(englishVoices)
      setSelectedVoiceURI((current) => current || englishVoices[0]?.voiceURI || '')
    }
    updateVoices()
    if (window.speechSynthesis) window.speechSynthesis.onvoiceschanged = updateVoices
    return () => {
      if (window.speechSynthesis) window.speechSynthesis.onvoiceschanged = null
      window.speechSynthesis?.cancel()
    }
  }, [])

  useEffect(() => {
    if (!audioUrl) return undefined
    return () => URL.revokeObjectURL(audioUrl)
  }, [audioUrl])

  useEffect(() => {
    if (page !== 'session' || !interview?.created_at) return undefined
    const startedAt = Date.parse(interview.created_at)
    const updateElapsed = () => setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)))
    updateElapsed()
    const interval = window.setInterval(updateElapsed, 1000)
    return () => window.clearInterval(interval)
  }, [page, interview?.id, interview?.created_at])

  useEffect(() => {
    if (!recording) return undefined
    const interval = window.setInterval(() => {
      setRecordingSeconds(Math.floor((Date.now() - recordingStartedAtRef.current) / 1000))
    }, 250)
    return () => window.clearInterval(interval)
  }, [recording])

  useEffect(() => () => {
    if (mediaRecorderRef.current?.state === 'recording') {
      discardOnRecorderStopRef.current = true
      mediaRecorderRef.current.stop()
    }
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop())
    window.speechSynthesis?.cancel()
  }, [])

  useEffect(() => {
    if (page === 'session' && interview?.id === routeInterviewId) return
    if (mediaRecorderRef.current?.state === 'recording') {
      discardOnRecorderStopRef.current = true
      mediaRecorderRef.current.stop()
    }
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop())
    mediaStreamRef.current = null
  }, [page, routeInterviewId, interview?.id])

  const refreshData = useCallback(async () => {
    const [summary, sessions] = await Promise.all([
      api('/dashboard'),
      api('/interviews'),
    ])
    setDashboard(summary)
    setInterviews(sessions)
  }, [])

  useEffect(() => {
    let active = true
    Promise.all([api('/dashboard'), api('/interviews'), api('/llm/status')])
      .then(([summary, sessions, modelStatus]) => {
        if (!active) return
        setDashboard(summary)
        setInterviews(sessions)
        setLlmStatus(modelStatus)
      })
      .catch((loadError) => {
        if (active) setError(loadError.message)
      })
      .finally(() => setLoading(false))
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    api('/mcp/status')
      .then((status) => {
        if (active) setMcpStatus(status)
      })
      .catch((statusError) => {
        if (active) setMcpStatus({ connected: false, error: statusError.message })
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!routeInterviewId || interview?.id === routeInterviewId) return undefined
    let active = true
    api(`/interviews/${routeInterviewId}`)
      .then((result) => {
        if (!active) return
        setInterview(result)
        const savedAnswer = result.answers.find((item) => item.question_index === result.current_index)
        setAnswer(savedAnswer?.answer || '')
        setLatestEvaluation(savedAnswer?.evaluation || null)
      })
      .catch((loadError) => {
        if (active) setError(loadError.message)
      })
    return () => { active = false }
  }, [routeInterviewId, interview?.id])

  const openInterview = async (id) => {
    setError('')
    try {
      const result = await api(`/interviews/${id}`)
      discardRecording(false)
      setInterview(result)
      const savedAnswer = result.answers.find((item) => item.question_index === result.current_index)
      setAnswer(savedAnswer?.answer || '')
      setLatestEvaluation(null)
      navigate(`${result.status === 'completed' ? 'results' : 'session'}/${id}`)
    } catch (openError) {
      setError(openError.message)
    }
  }

  const startInterview = async (event) => {
    event.preventDefault()
    discardRecording()
    setSaving(true)
    setError('')
    try {
      const created = await api('/interviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      })
      setInterview(created)
      setAnswer('')
      setLatestEvaluation(null)
      setForm(initialForm)
      navigate(`session/${created.id}`)
      await refreshData()
    } catch (createError) {
      setError(createError.message)
    } finally {
      setSaving(false)
    }
  }

  const uploadDocument = async (event, kind) => {
    const file = event.target.files?.[0]
    if (!file) return
    setUploading(kind)
    setError('')
    setDocumentAnalysis(null)
    const payload = new FormData()
    payload.append('file', file)
    payload.append('kind', kind)
    try {
      const result = await api('/parse-document', { method: 'POST', body: payload })
      setForm((previous) => ({ ...previous, [kind === 'resume' ? 'resume_text' : 'job_description']: result.text }))
    } catch (uploadError) {
      setError(uploadError.message)
    } finally {
      setUploading('')
      event.target.value = ''
    }
  }

  const analyzeDocuments = async () => {
    setAnalyzingDocuments(true)
    setError('')
    try {
      const result = await api('/analyze-documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          resume_text: form.resume_text,
          job_description: form.job_description,
        }),
      })
      setDocumentAnalysis(result)
    } catch (analysisError) {
      setError(analysisError.message)
    } finally {
      setAnalyzingDocuments(false)
    }
  }

  const submitAnswer = async (event) => {
    event.preventDefault()
    if (!interview || !answer.trim()) return
    setSaving(true)
    setError('')
    try {
      const result = await api(`/interviews/${interview.id}/answers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question_index: interview.current_index, answer: answer.trim() }),
      })
      setInterview(result.interview)
      setLatestEvaluation(result.evaluation)
      await refreshData()
      if (result.interview.status === 'completed') navigate(`results/${result.interview.id}`)
      else setAnswer('')
      discardRecording()
    } catch (submitError) {
      setError(submitError.message)
    } finally {
      setSaving(false)
    }
  }

  const moveQuestion = (direction) => {
    if (!interview) return
    const nextIndex = Math.max(0, Math.min(interview.questions.length - 1, interview.current_index + direction))
    setInterview((previous) => ({ ...previous, current_index: nextIndex }))
    const savedAnswer = interview.answers.find((item) => item.question_index === nextIndex)
    setAnswer(savedAnswer?.answer || '')
    setLatestEvaluation(savedAnswer?.evaluation || null)
    stopAndDiscardRecording()
  }

  const discardRecording = (clearAnswer = true) => {
    if (audioUrl) URL.revokeObjectURL(audioUrl)
    setAudioUrl('')
    setRecordedAudio(null)
    setRecordingSeconds(0)
    setMicrophoneError('')
    if (clearAnswer) setAnswer('')
  }

  const stopAndDiscardRecording = () => {
    if (mediaRecorderRef.current?.state === 'recording') {
      discardOnRecorderStopRef.current = true
      mediaRecorderRef.current.stop()
    }
    mediaStreamRef.current?.getTracks().forEach((track) => track.stop())
    mediaStreamRef.current = null
    discardRecording(false)
  }

  const startRecording = async () => {
    setMicrophoneError('')
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      setMicrophoneError('Audio recording is not supported by this browser. You can type your answer instead.')
      return
    }
    try {
      const requestedInterviewId = interview?.id
      const requestedQuestionIndex = interview?.current_index
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (
        pageRef.current !== 'session'
        || interviewRef.current?.id !== requestedInterviewId
        || interviewRef.current?.current_index !== requestedQuestionIndex
      ) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      discardOnRecorderStopRef.current = false
      mediaStreamRef.current = stream
      audioChunksRef.current = []
      const mimeType = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
        .find((type) => MediaRecorder.isTypeSupported(type))
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) audioChunksRef.current.push(event.data)
      }
      recorder.onerror = (event) => {
        setMicrophoneError(event.error?.message || 'The browser could not record audio. You can type your answer instead.')
        setRecording(false)
        stream.getTracks().forEach((track) => track.stop())
      }
      recorder.onstop = () => {
        if (discardOnRecorderStopRef.current || pageRef.current !== 'session') {
          discardOnRecorderStopRef.current = false
          stream.getTracks().forEach((track) => track.stop())
          mediaStreamRef.current = null
          setRecording(false)
          return
        }
        const blob = new Blob(audioChunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        if (blob.size > 0) {
          setRecordedAudio(blob)
          setAudioUrl(URL.createObjectURL(blob))
        }
        stream.getTracks().forEach((track) => track.stop())
        mediaStreamRef.current = null
        setRecordingSeconds(Math.floor((Date.now() - recordingStartedAtRef.current) / 1000))
        setRecording(false)
      }
      mediaRecorderRef.current = recorder
      recordingStartedAtRef.current = Date.now()
      setRecordingSeconds(0)
      setAnswer('')
      setRecordedAudio(null)
      setAudioUrl('')
      recorder.start(250)
      setRecording(true)
    } catch (recordError) {
      const message = recordError instanceof Error ? recordError.message : 'Could not access the microphone.'
      setMicrophoneError(
        recordError?.name === 'NotAllowedError' || recordError?.name === 'PermissionDeniedError'
          ? 'Microphone access was denied. Allow microphone access in your browser settings, or type your answer.'
          : `Could not start microphone recording: ${message}`,
      )
    }
  }

  const stopRecording = () => {
    if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop()
  }

  const transcribeRecording = async () => {
    if (!recordedAudio) return
    setTranscribing(true)
    setMicrophoneError('')
    const extension = recordedAudio.type.includes('mp4') ? 'm4a' : 'webm'
    const payload = new FormData()
    payload.append('file', recordedAudio, `answer.${extension}`)
    try {
      const result = await api('/transcribe', { method: 'POST', body: payload })
      setAnswer(result.text)
    } catch (transcriptionError) {
      setMicrophoneError(`${transcriptionError.message} You can type your answer below instead.`)
    } finally {
      setTranscribing(false)
    }
  }

  const speakQuestion = () => {
    if (!window.speechSynthesis || !interview || speechMuted) return
    const utterance = new SpeechSynthesisUtterance(interview.questions[interview.current_index])
    utterance.voice = availableVoices.find((voice) => voice.voiceURI === selectedVoiceURI) || null
    window.speechSynthesis.cancel()
    window.speechSynthesis.speak(utterance)
  }

  const toggleSpeechMuted = () => {
    setSpeechMuted((muted) => {
      if (!muted) window.speechSynthesis?.cancel()
      return !muted
    })
  }

  const elapsedLabel = formatDuration(elapsedSeconds)

  const navigate = (nextPage) => {
    setError('')
    if (page === 'session') stopAndDiscardRecording()
    routerNavigate(nextPage === 'dashboard' ? '/' : `/${nextPage}`)
  }

  if (loading) {
    return <div className="loading-screen"><span className="loader" />Preparing your workspace</div>
  }

  const currentQuestion = interview?.questions[interview.current_index]
  const currentQuestionDetail = interview?.question_details?.[interview.current_index]
  const answeredCount = interview?.answers.length || 0
  const scores = interview?.answers.map((item) => item.score) || []
  const averageScore = scores.length ? Math.round(scores.reduce((total, score) => total + score, 0) / scores.length) : 0
  const modelStatusLabel = !llmStatus
    ? 'Checking local model'
    : !llmStatus.available
      ? 'Ollama offline'
      : llmStatus.model_available
        ? 'Local LLM ready'
        : 'Model needs download'

  return (
    <div className="app-shell" data-theme={theme}>
      <aside className="sidebar">
        <button className="brand" onClick={() => navigate('dashboard')} aria-label="InterviewAI home">
          <span className="brand-mark"><Icon name="spark" size={22} /></span>
          <span>interview<span className="brand-ai">ai</span></span>
        </button>
        <div className="workspace-label">WORKSPACE</div>
        <nav className="side-nav" aria-label="Main navigation">
          <button className={page === 'dashboard' || page === 'setup' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('dashboard')}><Icon name="grid" />Overview</button>
          <button className={page === 'history' ? 'nav-item active' : 'nav-item'} onClick={() => navigate('history')}><Icon name="list" />My interviews<span className="nav-count">{interviews.length}</span></button>
        </nav>
        <div className="sidebar-bottom">
          <div className="local-card"><span className="local-dot" /><div><strong>Private by design</strong><small>Your practice stays on this device</small></div></div>
          <div className="profile"><div className="avatar">Y</div><div><strong>Your workspace</strong><small>Local account</small></div><span className="profile-menu">···</span></div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> <strong>{page === 'dashboard' ? 'Overview' : page === 'setup' ? 'New interview' : page === 'session' ? 'Practice session' : page === 'results' ? 'Session results' : 'My interviews'}</strong></div>
          <div className="topbar-right"><span className={`api-status ${llmStatus?.model_available ? 'ready' : 'not-ready'}`} title={llmStatus?.model || 'Ollama'}><span />{modelStatusLabel}</span><span className={`api-status mcp-status ${mcpStatus?.connected ? 'ready' : 'not-ready'}`} title={mcpStatus?.connected ? mcpStatus.tools?.map((tool) => tool.name).join(', ') : mcpStatus?.error || 'Checking MCP server'}><span />{mcpStatus?.connected ? 'MCP connected' : 'MCP offline'}</span><button className="theme-toggle" type="button" onClick={() => setTheme((current) => current === 'light' ? 'dark' : 'light')} aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`} title={`Switch to ${theme === 'light' ? 'dark' : 'light'} mode`}><Icon name={theme === 'light' ? 'moon' : 'sun'} size={17} /></button><div className="top-avatar">Y</div></div>
        </header>

        <div className="page-content">
          {error && <div className="alert" role="alert"><span>{error}</span><button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
          {(page === 'session' || page === 'results') && !routeInterviewId && <div className="panel route-message"><p>This interview link is missing its session ID.</p><button className="button button-outline" onClick={() => navigate('dashboard')}>Back to overview</button></div>}
          {routeInterviewId && interview?.id !== routeInterviewId && <div className="panel route-message"><span className="loader" />Loading saved interview…</div>}

          {page === 'dashboard' && (
            <>
              <section className="welcome-row">
                <div><p className="eyebrow">{formatToday()}</p><h1>Your next opportunity<br />starts with practice.</h1><p className="welcome-copy">Build confidence one thoughtful answer at a time.</p></div>
                <button className="button button-primary" onClick={() => navigate('setup')}><Icon name="plus" size={19} />Start an interview</button>
              </section>
              <section className="stats-grid" aria-label="Practice statistics">
                <StatCard label="Total interviews" value={dashboard?.total_interviews ?? 0} hint="Practice makes progress" icon="grid" />
                <StatCard label="Completed" value={dashboard?.completed_interviews ?? 0} hint="Sessions finished" icon="check" />
                <StatCard label="Average score" value={dashboard?.average_score ? `${dashboard.average_score}%` : '—'} hint="Across completed sessions" icon="trend" />
                <StatCard label="Practice time" value={`${dashboard?.practice_minutes ?? 0}m`} hint="Estimated time invested" icon="clock" />
              </section>
              <ProgressPanel
                progress={dashboard?.score_progress ?? []}
                dimensions={dashboard?.score_dimensions ?? []}
                onOpen={openInterview}
              />
              <section className="dashboard-grid">
                <div className="panel recent-panel">
                  <div className="panel-heading"><div><p className="eyebrow">KEEP YOUR MOMENTUM</p><h2>Recent interviews</h2></div><button className="text-button" onClick={() => navigate('history')}>View all <Icon name="arrow" size={16} /></button></div>
                  {dashboard?.recent_interviews?.length ? (
                    <div className="recent-list">{dashboard.recent_interviews.slice(0, 4).map((item) => <InterviewRow key={item.id} interview={item} onClick={() => openInterview(item.id)} />)}</div>
                  ) : <EmptyState compact onStart={() => navigate('setup')} />}
                </div>
                <div className="panel focus-panel">
                  <div className="focus-icon"><Icon name="spark" size={24} /></div>
                  <p className="eyebrow">YOUR PRACTICE SPACE</p>
                  <h2>Show up as your<br />most prepared self.</h2>
                  <p>Practice role-specific questions, speak your answers out loud, and get useful feedback after every response.</p>
                  <div className="focus-benefits"><span><Icon name="check" size={15} />No cloud API bill</span><span><Icon name="check" size={15} />Private & local</span></div>
                  <button className="button button-dark" onClick={() => navigate('setup')}>Set up a practice <Icon name="arrow" size={17} /></button>
                </div>
              </section>
              <section className="tip-strip"><div className="tip-icon"><Icon name="spark" size={20} /></div><div><strong>A little structure goes a long way</strong><p>Try the STAR method: Situation, Task, Action, Result. Specific examples help your answers stand out.</p></div><span className="tip-tag">QUICK TIP</span></section>
            </>
          )}

          {page === 'setup' && (
            <section className="setup-layout">
              <div className="setup-heading"><button className="back-link" onClick={() => navigate('dashboard')}>← Back to overview</button><p className="eyebrow">MAKE IT YOURS</p><h1>Set up your interview</h1><p>We’ll tailor a focused practice session around the role you’re targeting.</p></div>
              {!llmStatus?.model_available && <div className="llm-notice" role="status"><Icon name="spark" size={17} /><span><strong>{llmStatus?.message || 'Checking that your local LLM is ready.'}</strong><small>{!llmStatus?.available ? 'Start Ollama, then refresh this page.' : `Run “ollama pull ${llmStatus?.model || 'llama3.2:3b'}” in PowerShell, then refresh this page.`} Interviews and answer feedback require the local model; your documents are not sent to a cloud provider.</small></span></div>}
              <form className="panel setup-form" onSubmit={startInterview}>
                <div className="form-section"><div className="section-number">01</div><div className="form-section-content"><h2>The role</h2><p>Start with the position you’re preparing for.</p><label className="field-label" htmlFor="role">Target role <span>Required</span></label><input id="role" className="text-input" required minLength="2" maxLength="100" placeholder="e.g. Product Designer, Software Engineer" value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })} /><div className="two-fields"><label className="field-label">Experience level<select className="text-input" value={form.experience_level} onChange={(event) => setForm({ ...form, experience_level: event.target.value })}><option>Entry-level</option><option>Mid-level</option><option>Senior</option><option>Lead / Staff</option></select></label><label className="field-label">Interview style<select className="text-input" value={form.interview_type} onChange={(event) => setForm({ ...form, interview_type: event.target.value })}><option>Mixed</option><option>Technical</option><option>Behavioral</option></select></label></div><label className="field-label count-label">Questions <strong>{form.question_count}</strong></label><input className="range-input" type="range" min="3" max="10" value={form.question_count} onChange={(event) => setForm({ ...form, question_count: Number(event.target.value) })} /><div className="range-ends"><span>3 questions</span><span>10 questions</span></div></div></div>
                <div className="form-section"><div className="section-number">02</div><div className="form-section-content"><h2>Add context <span className="optional-label">OPTIONAL</span></h2><p>Give your practice a personal touch with a resume or job description.</p><label className="field-label" htmlFor="resume">Resume</label><FileUpload id="resume" kind="resume" uploading={uploading} onFile={(event) => uploadDocument(event, 'resume')} /><textarea id="resume" className="text-input context-input" placeholder="Or paste your resume text here..." maxLength="30000" value={form.resume_text} onChange={(event) => { setForm({ ...form, resume_text: event.target.value }); setDocumentAnalysis(null) }} /><label className="field-label" htmlFor="job-description">Job description</label><FileUpload id="job-description" kind="job_description" uploading={uploading} onFile={(event) => uploadDocument(event, 'job_description')} /><textarea id="job-description" className="text-input context-input" placeholder="Or paste the job description here..." maxLength="15000" value={form.job_description} onChange={(event) => { setForm({ ...form, job_description: event.target.value }); setDocumentAnalysis(null) }} /><button className="button button-outline document-analysis-button" type="button" onClick={analyzeDocuments} disabled={analyzingDocuments || (!form.resume_text.trim() && !form.job_description.trim())}>{analyzingDocuments ? 'Analyzing requirements…' : 'Compare resume with job requirements'}</button>{documentAnalysis && <div className="document-analysis" role="status"><strong>Role fit snapshot</strong><div><span>Matching skills</span><b>{documentAnalysis.matched_skills.length ? documentAnalysis.matched_skills.join(', ') : 'No direct matches detected'}</b></div><div><span>Skills to develop</span><b>{documentAnalysis.missing_skills.length ? documentAnalysis.missing_skills.join(', ') : 'No additional listed skills detected'}</b></div>{documentAnalysis.minimum_experience_years != null && <div><span>Experience requested</span><b>{documentAnalysis.minimum_experience_years}+ years</b></div>}</div>}<p className="privacy-note"><Icon name="check" size={15} /> Documents are parsed locally and used only to tailor this session.</p><p className="privacy-note">Audio is recorded and transcribed locally; use the text answer option if microphone access is unavailable.</p></div></div>
                <div className="setup-footer"><span>Ready when you are. Your practice is saved on this device.</span><button className="button button-primary" disabled={saving || Boolean(uploading)} type="submit">{saving ? 'Preparing…' : 'Begin interview'}<Icon name="arrow" size={17} /></button></div>
              </form>
            </section>
          )}

          {page === 'session' && interview && currentQuestion && interview.id === routeInterviewId && (
            <section className="session-layout">
              {interview.generation_source === 'fallback' && <div className="llm-notice fallback-notice" role="status"><Icon name="spark" size={17} /><span><strong>Using reliable backup questions.</strong><small>The local model returned invalid question data, so this session uses validated role-specific questions.</small></span></div>}
              <div className="session-top"><div><p className="eyebrow">{interview.role.toUpperCase()} · {interview.interview_type.toUpperCase()}</p><h1>Practice in your own words.</h1></div><button className="button button-quiet" onClick={() => navigate('dashboard')}>Save & exit</button></div>
              <div className="session-progress"><div><span>QUESTION {String(interview.current_index + 1).padStart(2, '0')} <span className="muted">OF {String(interview.questions.length).padStart(2, '0')}</span></span><span className="session-timer"><Icon name="timer" size={13} />{elapsedLabel} elapsed</span><span>{Math.round((answeredCount / interview.questions.length) * 100)}% complete</span></div><div className="progress-track"><span style={{ width: `${Math.max(4, (answeredCount / interview.questions.length) * 100)}%` }} /></div></div>
              <div className="session-columns">
                <div className="panel question-panel">
                  <div className="question-label"><span className="question-pulse" />YOUR QUESTION{currentQuestionDetail?.is_follow_up && <span className="follow-up-tag">FOLLOW-UP</span>}</div>
                  <h2>{currentQuestion}</h2>
                  {currentQuestionDetail?.competency && <div className="competency-label">FOCUS: {currentQuestionDetail.competency}</div>}
                  <div className="question-actions">
                    <div className="speech-controls">
                      <button className="button button-outline" type="button" onClick={speakQuestion} disabled={speechMuted || !window.speechSynthesis}><Icon name="volume" size={17} />Speak question</button>
                      <button className="button button-outline mute-button" type="button" onClick={toggleSpeechMuted} disabled={!window.speechSynthesis} aria-pressed={speechMuted}><Icon name={speechMuted ? 'volumeMute' : 'volume'} size={15} />{speechMuted ? 'Unmute' : 'Mute'}</button>
                      <label className="voice-select-label" htmlFor="interviewer-voice">Voice<select id="interviewer-voice" value={selectedVoiceURI} onChange={(event) => setSelectedVoiceURI(event.target.value)} disabled={!availableVoices.length}><option value="">Browser default</option>{availableVoices.map((voice) => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} ({voice.lang})</option>)}</select></label>
                    </div>
                    <span>Listen, then answer aloud or type.</span>
                  </div>
                  <div className="answer-wrap">
                    <div className="answer-heading"><label htmlFor="answer">{recordedAudio && answer ? 'Transcript (editable)' : 'Your answer'}</label><span>{answer.trim() ? answer.trim().split(/\s+/).length : 0} words</span></div>
                    <textarea id="answer" className="answer-input" placeholder="Start with the context, then walk through what you did and what happened..." value={answer} onChange={(event) => setAnswer(event.target.value)} />
                    <div className="recording-controls">
                      {recording
                        ? <button className="voice-button recording-active" type="button" onClick={stopRecording}><Icon name="stop" size={15} />Stop recording <span>{formatDuration(recordingSeconds)}</span></button>
                        : <button className="voice-button" type="button" onClick={startRecording}><Icon name="mic" size={16} />Start recording</button>}
                      {recordedAudio && !recording && <>
                        <span className="recording-duration">Recorded {formatDuration(recordingSeconds)}</span>
                        <audio className="recorded-audio" controls src={audioUrl} aria-label="Replay your recorded answer" />
                        <button className="voice-button" type="button" onClick={discardRecording}><Icon name="retry" size={15} />Retake</button>
                        <button className="voice-button transcribe-button" type="button" onClick={transcribeRecording} disabled={transcribing}><Icon name="play" size={15} />{transcribing ? 'Transcribing locally…' : 'Transcribe locally'}</button>
                      </>}
                    </div>
                    <p className="recording-note">Recordings are transcribed on this device with Faster-Whisper and are not saved to your interview history. You can always type your answer instead.</p>
                    {microphoneError && <p className="recording-error" role="alert">{microphoneError}</p>}
                  </div>
                  <div className="session-footer">
                    {interview.current_index > 0 ? <button className="button button-quiet" type="button" onClick={() => moveQuestion(-1)}>← Previous</button> : <span />}
                    <button className="button button-primary" type="button" onClick={submitAnswer} disabled={saving || transcribing || recording || !answer.trim()}>{saving ? 'Evaluating with Ollama…' : interview.current_index === interview.questions.length - 1 ? 'Finish interview' : 'Save & next'}<Icon name="arrow" size={17} /></button>
                  </div>
                </div>
                <aside className="session-aside">
                  <div className="panel aside-card"><p className="eyebrow">WHAT THIS QUESTION ASSESSES</p><h3>{currentQuestionDetail?.competency || 'Communication'}</h3><div className="criteria-list">{(currentQuestionDetail?.evaluation_criteria || ['Relevance', 'Structure', 'Specificity']).map((criterion) => <span key={criterion}><Icon name="check" size={14} />{criterion}</span>)}</div></div>
                  <div className="panel feedback-preview">{latestEvaluation ? <><p className="eyebrow">LAST ANSWER · OLLAMA FEEDBACK</p><div className="score-line"><strong>{latestEvaluation.score}</strong><span>/ 100</span><span className="score-meter"><i style={{ width: `${latestEvaluation.score}%` }} /></span></div>{latestEvaluation.improvements?.[0] && <p>{latestEvaluation.improvements[0]}</p>}{latestEvaluation.suggested_answer && <details className="suggested-answer"><summary>View suggested answer</summary><p>{latestEvaluation.suggested_answer}</p></details>}</> : <><div className="feedback-spark"><Icon name="spark" size={18} /></div><strong>Feedback tailored to your answer.</strong><p>Ollama will score your response against the role-specific criteria and explain how to improve it.</p></>}</div>
                </aside>
              </div>
            </section>
          )}

          {page === 'results' && interview && interview.id === routeInterviewId && (
            <section className="results-layout">
              <button className="back-link" onClick={() => navigate('history')}>← All interviews</button>
              <div className="results-hero">
                <div><p className="eyebrow">SESSION COMPLETE</p><h1>You showed up. That matters.</h1><p>Here’s a snapshot of your {interview.role} practice.</p></div>
                <div className="results-score"><strong>{averageScore}</strong><span>AVERAGE SCORE</span></div>
              </div>
              <div className="results-summary">
                <div><strong>{interview.answers.length}<small>/{interview.questions.length}</small></strong><span>Questions answered</span></div>
                <div><strong>{interview.answers.reduce((sum, item) => sum + item.evaluation.word_count, 0)}</strong><span>Words practiced</span></div>
                <div><strong>{interview.experience_level}</strong><span>Experience level</span></div>
              </div>
              <div className="panel review-panel">
                <div className="panel-heading">
                  <div><p className="eyebrow">ANSWER BY ANSWER</p><h2>Your feedback</h2></div>
                  <span className="baseline-tag">LOCAL OLLAMA EVALUATOR</span>
                </div>
                <div className="review-list">
                  {interview.questions.map((question, index) => {
                    const item = interview.answers.find((entry) => entry.question_index === index)
                    const details = interview.question_details?.[index]
                    return (
                      <article className="review-item" key={`${interview.id}-${index}`}>
                        <div className="review-question"><span>{String(index + 1).padStart(2, '0')}</span><h3>{details?.is_follow_up && <small className="follow-up-tag">FOLLOW-UP</small>}{question}</h3>{item && <b>{item.score}</b>}</div>
                        {item ? (
                          <>
                            {item.evaluation.criteria?.length > 0 && <div className="rubric-list"><strong>Scored on four answer dimensions</strong>{item.evaluation.criteria.map((criterion) => <div className="rubric-item" key={criterion.name}><span>{criterion.name}</span><b>{criterion.score}</b><p>{criterion.feedback}</p></div>)}</div>}
                            <div className="review-details">
                              <div><strong>What worked</strong>{(item.evaluation.strengths || []).map((text) => <p key={text}><Icon name="check" size={14} />{text}</p>)}</div>
                              <div><strong>Try next time</strong>{(item.evaluation.improvements || []).map((text) => <p key={text}>{text}</p>)}</div>
                            </div>
                            {item.evaluation.suggested_answer && <details className="suggested-answer results-suggestion"><summary>See a suggested answer</summary><p>{item.evaluation.suggested_answer}</p></details>}
                          </>
                        ) : (
                          <p className="unanswered">{details?.competency ? `Focus: ${details.competency}. ` : ''}Not answered in this session.</p>
                        )}
                      </article>
                    )
                  })}
                </div>
              </div>
              <div className="results-actions"><button className="button button-outline" onClick={() => openInterview(interview.id)}>Review session</button><button className="button button-primary" onClick={() => navigate('setup')}><Icon name="plus" size={18} />Practice again</button></div>
            </section>
          )}

          {page === 'history' && (
            <section className="history-layout"><div className="history-heading"><div><p className="eyebrow">YOUR PRACTICE, ALL IN ONE PLACE</p><h1>My interviews</h1><p>Every session is saved locally so you can keep track of your progress.</p></div><button className="button button-primary" onClick={() => navigate('setup')}><Icon name="plus" size={18} />New interview</button></div><div className="panel history-panel">{interviews.length ? <><div className="history-table-head"><span>ROLE</span><span>TYPE</span><span>DATE</span><span>STATUS</span><span>SCORE</span><span /></div>{interviews.map((item) => <InterviewRow key={item.id} interview={item} onClick={() => openInterview(item.id)} detailed />)}</> : <EmptyState onStart={() => navigate('setup')} />}</div></section>
          )}
        </div>
      </main>
    </div>
  )
}

function StatCard({ label, value, hint, icon }) {
  return <div className="panel stat-card"><div className="stat-top"><span>{label}</span><span className="stat-icon"><Icon name={icon} size={17} /></span></div><strong>{value}</strong><small>{hint}</small></div>
}

function ProgressPanel({ progress, dimensions, onOpen }) {
  const points = progress.map((item, index) => ({
    ...item,
    x: progress.length === 1 ? 300 : 24 + index * (552 / (progress.length - 1)),
    y: 150 - (item.score / 100) * 125,
  }))

  return (
    <section className="panel progress-panel" aria-labelledby="progress-heading">
      <div className="progress-chart">
        <div className="panel-heading">
          <div><p className="eyebrow">YOUR PERFORMANCE</p><h2 id="progress-heading">Score progress</h2></div>
          <span className="progress-count">{progress.length} recent {progress.length === 1 ? 'session' : 'sessions'}</span>
        </div>
        {points.length ? (
          <svg className="progress-svg" viewBox="0 0 600 190" role="img" aria-label={`Scores across ${progress.length} completed interviews`}>
            {[25, 87.5, 150].map((y, index) => (
              <g key={y}>
                <line x1="24" x2="576" y1={y} y2={y} className="chart-gridline" />
                <text x="2" y={y + 4} className="chart-axis-label">{100 - index * 50}</text>
              </g>
            ))}
            {points.length > 1 && <polyline points={points.map((point) => `${point.x},${point.y}`).join(' ')} className="chart-line" />}
            {points.map((point) => (
              <g key={point.id} className="chart-point-group">
                <circle cx={point.x} cy={point.y} r="5" className="chart-point" />
                <text x={point.x} y={point.y - 12} textAnchor="middle" className="chart-score-label">{point.score}%</text>
                <text x={point.x} y="177" textAnchor="middle" className="chart-date-label">{formatDate(point.created_at)}</text>
                <title>{`${point.role}: ${point.score}%`}</title>
              </g>
            ))}
          </svg>
        ) : (
          <div className="progress-empty">Complete an interview to see your score trend.</div>
        )}
        {points.length > 0 && (
          <ol className="sr-only">
            {points.map((point) => <li key={point.id}>{point.role}: {point.score}% on {formatDate(point.created_at)}</li>)}
          </ol>
        )}
      </div>
      <div className="scorecard-summary" aria-label="Average scorecard by criterion">
        <p className="eyebrow">AVERAGE SCORECARD</p>
        {dimensions.some((dimension) => dimension.answer_count > 0) ? (
          dimensions.map((dimension) => (
            <div className="dimension-score" key={dimension.name}>
              <div><span>{dimension.name}</span><strong>{dimension.answer_count ? `${dimension.score}%` : '—'}</strong></div>
              <div className="dimension-meter" role="progressbar" aria-label={`${dimension.name} average`} aria-valuemin="0" aria-valuemax="100" aria-valuenow={dimension.answer_count ? dimension.score : 0}>
                <i style={{ width: `${dimension.score}%` }} />
              </div>
            </div>
          ))
        ) : (
          <p className="scorecard-empty">Complete a session with the current rubric to see criterion averages.</p>
        )}
        {points.length > 0 && <button className="text-button progress-latest" onClick={() => onOpen(points[points.length - 1].id)}>Review latest scorecard <Icon name="arrow" size={15} /></button>}
      </div>
    </section>
  )
}

function InterviewRow({ interview, onClick, detailed = false }) {
  return <button className={detailed ? 'interview-row detailed' : 'interview-row'} onClick={onClick}><span className="role-cell"><span className="role-icon"><Icon name="spark" size={17} /></span><span><strong>{interview.role}</strong>{detailed && <small>{interview.experience_level}</small>}</span></span><span className="type-cell">{interview.interview_type}</span><span className="date-cell">{formatDate(interview.created_at)}</span><span className={interview.status === 'completed' ? 'status-badge complete' : 'status-badge in-progress'}>{interview.status === 'completed' ? 'Completed' : 'In progress'}</span><span className="score-cell">{interview.average_score == null ? '—' : `${interview.average_score}%`}</span><span className="row-arrow">↗</span></button>
}

function FileUpload({ id, kind, uploading, onFile }) {
  return <label className="file-upload" htmlFor={`${id}-file`}><span className="upload-icon"><Icon name="upload" size={17} /></span><span><strong>{uploading === kind ? 'Reading file…' : 'Upload a file'}</strong><small>PDF, TXT or MD · up to 10 MB</small></span><input id={`${id}-file`} type="file" accept=".pdf,.txt,.md,application/pdf,text/plain,text/markdown" onChange={onFile} disabled={Boolean(uploading)} /></label>
}

function EmptyState({ onStart, compact = false }) {
  return <div className={compact ? 'empty-state compact' : 'empty-state'}><div className="empty-icon"><Icon name="spark" size={22} /></div><h3>Your first interview is waiting.</h3><p>Choose a role and we’ll create a set of questions to get you started.</p><button className="button button-outline" onClick={onStart}>Set up your first interview <Icon name="arrow" size={16} /></button></div>
}

export default App
