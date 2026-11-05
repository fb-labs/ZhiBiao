export type Account = {
  id: string;
  username: string;
  email: string | null;
  displayName: string;
  isSuperuser: boolean;
  roles: string[];
  permissions: string[];
};

export type AnalysisIssue = { level: string; title: string; detail: string };

export type Analysis = {
  id: string;
  tenderId: string;
  revision: number;
  model: string;
  overview: string;
  dimensions: { key: string; label: string; value: string; note: string }[];
  scoring: { method: string; total: number; items: { name: string; points: number; criteria: string }[] };
  risks: AnalysisIssue[];
  requirements: string[];
  createdAt: string | null;
};

export type Section = {
  id: string;
  documentId: string;
  position: number;
  parentKey: string | null;
  heading: string;
  intent: string;
  targetWords: number;
  body: string;
  wordCount: number;
  status: string;
  updatedAt: string | null;
};

export type BidDocument = {
  id: string;
  tenderId: string;
  title: string;
  status: string;
  sections: Section[];
};

export type ReviewIssue = {
  id: string;
  reviewId: string;
  code: string;
  severity: string;
  category: string;
  location: string;
  finding: string;
  recommendation: string;
  status: string;
};

export type Review = {
  id: string;
  tenderId: string;
  documentId: string | null;
  mode: string;
  status: string;
  score: number;
  verdict: string;
  summary: string;
  payload: Record<string, unknown>;
  createdAt: string | null;
  issues?: ReviewIssue[];
};

export type FixProposal = {
  id: string;
  issueId: string;
  sectionId: string;
  sectionHeading: string;
  originalText: string;
  proposedText: string;
  applied: boolean;
};

export type Delivery = {
  id: string;
  tenderId: string;
  documentId: string | null;
  format: string;
  filename: string;
  size: number;
  status: string;
  createdAt: string | null;
  downloadUrl: string;
};

export type GenerationIssue = {
  sectionId: string | null;
  heading: string;
  severity: string;
  category: string;
  message: string;
};

export type GenerationJob = {
  id: string;
  tenderId: string;
  kind: string;
  status: string;
  progress: number;
  message: string;
  payload: Record<string, unknown>;
  result: {
    generated?: number;
    failed?: number;
    skipped?: number;
    errors?: { sectionId: string; heading: string; error: string }[];
    quality?: { sections: number; written: number; blockers: number; issues: number; characters: number };
    issues?: GenerationIssue[];
    agent?: string;
    trace?: AgentTraceStep[];
  };
  createdAt: string | null;
  updatedAt: string | null;
};

export type QualityReport = {
  applied: number;
  stats: { sections: number; written: number; blockers: number; issues: number; characters: number };
  issues: GenerationIssue[];
};

export type AgentSpec = {
  key: string;
  name: string;
  description: string;
  maxSteps: number;
  assistant?: string;
  tools: { name: string; description: string }[];
};

export type AgentTraceStep = {
  step: number;
  thought: string;
  action: string;
  args: Record<string, unknown>;
  result?: unknown;
};

export type Template = {
  id: string;
  name: string;
  description: string;
  config: Record<string, unknown>;
  isDefault: boolean;
  createdAt: string | null;
};

export type AdminAccount = {
  id: string;
  username: string;
  email: string | null;
  displayName: string;
  isActive: boolean;
  isSuperuser: boolean;
  roles: string[];
  createdAt: string | null;
};

export type RoleInfo = {
  code: string;
  name: string;
  description: string;
  permissions: string[];
};

export type AuditEvent = {
  id: string;
  actor: string;
  action: string;
  targetType: string;
  targetId: string;
  detail: Record<string, unknown>;
  createdAt: string | null;
};

export type Tender = {
  id: string;
  code: string;
  title: string;
  buyer: string;
  status: string;
  budget: string;
  deadline: string;
  summary: string;
  createdAt: string | null;
  updatedAt: string | null;
  fileCount?: number;
  hasAnalysis?: boolean;
  hasOutline?: boolean;
  documentId?: string | null;
  sectionCount?: number;
  reviewId?: string | null;
};

export type TenderFile = {
  id: string;
  tenderId: string;
  role: string;
  filename: string;
  size: number;
  mime: string;
  parseState: string;
  createdAt: string | null;
  text?: string;
};

export type Provider = {
  id: string;
  label: string;
  baseUrl: string;
  apiKey: string;
  apiKeySet: boolean;
  model: string;
  temperature: number;
  maxTokens: number;
  isActive: boolean;
  isDefault: boolean;
};

export type Assistant = {
  id: string;
  key: string;
  label: string;
  purpose: string;
  providerId: string | null;
  model: string;
  temperature: number;
  maxTokens: number;
  enabled: boolean;
};

export type Overview = {
  total: number;
  byStatus: Record<string, number>;
  files: number;
  documents: number;
  sections: number;
  reviews: number;
  deliveries: number;
  monthly: { month: string; count: number }[];
  recent: { id: string; code: string; title: string; status: string; createdAt: string | null }[];
};

const BASE = '/api';

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...(init.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function streamSection(
  tenderId: string,
  sectionId: string,
  onDelta: (text: string) => void,
  onDone: (section: Section) => void,
): Promise<void> {
  const response = await fetch(`${BASE}/tenders/${tenderId}/sections/${sectionId}/generate`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!response.ok) {
    let detail = `生成失败 (${response.status})`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      // ignore
    }
    throw new Error(detail);
  }
  if (!response.body) throw new Error('浏览器不支持流式响应');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finished = false;
  while (!finished) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || '';
    for (const part of parts) {
      const line = part.split('\n').find((item) => item.startsWith('data:'));
      if (!line) continue;
      let payload: { delta?: string; done?: boolean; error?: string; section?: Section };
      try {
        payload = JSON.parse(line.slice(5).trim());
      } catch {
        continue;
      }
      if (payload.error) {
        throw new Error(payload.error);
      }
      if (payload.delta) onDelta(payload.delta);
      if (payload.done && payload.section) {
        onDone(payload.section);
        finished = true;
        break;
      }
    }
  }
}

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string; account: Account }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  me: () => request<Account>('/auth/me'),
  logout: () => request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),
  changePassword: (password: string) =>
    request<{ ok: boolean }>('/auth/password', {
      method: 'PUT',
      body: JSON.stringify({ password }),
    }),

  tenders: (query = '') => request<{ items: Tender[]; total: number }>(`/tenders${query}`),
  createTender: (payload: { title: string; buyer?: string; budget?: string; deadline?: string }) =>
    request<Tender>('/tenders', { method: 'POST', body: JSON.stringify(payload) }),
  tender: (id: string) => request<Tender>(`/tenders/${id}`),
  patchTender: (id: string, payload: Partial<Tender>) =>
    request<Tender>(`/tenders/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTender: (id: string) => request<{ ok: boolean }>(`/tenders/${id}`, { method: 'DELETE' }),

  files: (id: string) => request<{ items: TenderFile[] }>(`/tenders/${id}/files`),
  uploadFile: (id: string, file: File, role: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('role', role);
    return request<TenderFile>(`/tenders/${id}/files`, { method: 'POST', body: form });
  },
  fileText: (tenderId: string, fileId: string) =>
    request<TenderFile>(`/tenders/${tenderId}/files/${fileId}/text`),
  deleteFile: (tenderId: string, fileId: string) =>
    request<{ ok: boolean }>(`/tenders/${tenderId}/files/${fileId}`, { method: 'DELETE' }),

  overview: () => request<Overview>('/stats/overview'),

  analysis: (id: string) => request<Analysis>(`/tenders/${id}/analysis`),
  runAnalysis: (id: string) => request<Analysis>(`/tenders/${id}/analysis`, { method: 'POST' }),

  outline: (id: string) =>
    request<BidDocument & { revision: number; mode?: string; note?: string; targetWords?: number; createdAt: string | null }>(
      `/tenders/${id}/outline`,
    ),
  buildOutline: (id: string, options: { mode: string; targetWords: number }) =>
    request<BidDocument & { revision: number; mode?: string; note?: string; targetWords?: number; createdAt: string | null }>(
      `/tenders/${id}/outline`,
      { method: 'POST', body: JSON.stringify(options) },
    ),
  document: (id: string) => request<BidDocument>(`/tenders/${id}/document`),
  addSection: (tenderId: string, payload: { heading: string; intent?: string; targetWords?: number }) =>
    request<Section>(`/tenders/${tenderId}/sections`, { method: 'POST', body: JSON.stringify(payload) }),
  deleteSection: (tenderId: string, sectionId: string) =>
    request<{ ok: boolean }>(`/tenders/${tenderId}/sections/${sectionId}`, { method: 'DELETE' }),
  moveSection: (tenderId: string, sectionId: string, direction: 'up' | 'down') =>
    request<{ ok: boolean } | Section>(`/tenders/${tenderId}/sections/${sectionId}/move`, {
      method: 'POST',
      body: JSON.stringify({ direction }),
    }),
  startGeneration: (tenderId: string, payload: { mode: string; sectionIds?: string[] }) =>
    request<GenerationJob>(`/tenders/${tenderId}/generation`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  generationState: (tenderId: string) =>
    request<{ active: GenerationJob | null; items: GenerationJob[] }>(`/tenders/${tenderId}/generation`),
  generation: (jobId: string) => request<GenerationJob>(`/generation/${jobId}`),
  cancelGeneration: (jobId: string) =>
    request<{ ok: boolean }>(`/generation/${jobId}/cancel`, { method: 'POST' }),
  postprocess: (tenderId: string) =>
    request<QualityReport>(`/tenders/${tenderId}/generation/postprocess`, { method: 'POST' }),

  agents: () => request<{ items: AgentSpec[] }>('/agents'),
  patchSection: (tenderId: string, sectionId: string, payload: { heading?: string; intent?: string; body?: string }) =>
    request<Section>(`/tenders/${tenderId}/sections/${sectionId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  streamSection,

  reviews: (id: string) => request<{ items: Review[] }>(`/tenders/${id}/reviews`),
  review: (reviewId: string) => request<Review>(`/reviews/${reviewId}`),
  runReview: (id: string, mode: string) =>
    request<Review>(`/tenders/${id}/reviews`, { method: 'POST', body: JSON.stringify({ mode }) }),
  patchIssue: (reviewId: string, issueId: string, status: string) =>
    request<ReviewIssue>(`/reviews/${reviewId}/issues/${issueId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  proposeFix: (reviewId: string, issueId: string) =>
    request<FixProposal>(`/reviews/${reviewId}/issues/${issueId}/fix`, { method: 'POST' }),
  applyFix: (reviewId: string, issueId: string, proposalId: string) =>
    request<{ ok: boolean; sectionId: string; body: string }>(
      `/reviews/${reviewId}/issues/${issueId}/fix/${proposalId}/apply`,
      { method: 'POST' },
    ),

  deliveries: (id: string) => request<{ items: Delivery[] }>(`/tenders/${id}/deliveries`),
  createDelivery: (id: string, format: string) =>
    request<Delivery>(`/tenders/${id}/deliveries`, { method: 'POST', body: JSON.stringify({ format }) }),

  templates: () => request<{ items: Template[]; defaults: Record<string, unknown> }>('/templates'),
  createTemplate: (payload: Record<string, unknown>) =>
    request<Template>('/templates', { method: 'POST', body: JSON.stringify(payload) }),
  updateTemplate: (id: string, payload: Record<string, unknown>) =>
    request<Template>(`/templates/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteTemplate: (id: string) => request<{ ok: boolean }>(`/templates/${id}`, { method: 'DELETE' }),

  accounts: () => request<{ items: AdminAccount[] }>('/admin/accounts'),
  roles: () => request<{ items: RoleInfo[] }>('/admin/roles'),
  permissions: () => request<{ permissions: { code: string; name: string; group: string }[] }>('/admin/permissions'),
  createAccount: (payload: Record<string, unknown>) =>
    request<AdminAccount>('/admin/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  updateAccount: (id: string, payload: Record<string, unknown>) =>
    request<AdminAccount>(`/admin/accounts/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteAccount: (id: string) => request<{ ok: boolean }>(`/admin/accounts/${id}`, { method: 'DELETE' }),
  audit: () => request<{ items: AuditEvent[] }>('/admin/audit?limit=50'),

  providers: () => request<{ items: Provider[] }>('/admin/providers'),
  createProvider: (payload: Record<string, unknown>) =>
    request<Provider>('/admin/providers', { method: 'POST', body: JSON.stringify(payload) }),
  updateProvider: (id: string, payload: Record<string, unknown>) =>
    request<Provider>(`/admin/providers/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteProvider: (id: string) =>
    request<{ ok: boolean }>(`/admin/providers/${id}`, { method: 'DELETE' }),
  probeProvider: (id: string) =>
    request<{ ok: boolean; latencyMs: number; reply?: string; error?: string }>(
      `/admin/providers/${id}/probe`,
      { method: 'POST' },
    ),

  assistants: () => request<{ items: Assistant[] }>('/admin/assistants'),
  createAssistant: (payload: Record<string, unknown>) =>
    request<Assistant>('/admin/assistants', { method: 'POST', body: JSON.stringify(payload) }),
  updateAssistant: (id: string, payload: Record<string, unknown>) =>
    request<Assistant>(`/admin/assistants/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteAssistant: (id: string) =>
    request<{ ok: boolean }>(`/admin/assistants/${id}`, { method: 'DELETE' }),
};
