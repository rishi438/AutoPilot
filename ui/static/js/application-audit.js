(function () {
    'use strict';
    const API_BASE = (window.APP_CONFIG && window.APP_CONFIG.apiBase) || '/api/v1';
    const root = document.querySelector('[data-application-id]');
    const applicationId = root ? String(root.dataset.applicationId || '') : '';
    const token = () => window.app && typeof window.app.getAuthToken === 'function' ? window.app.getAuthToken() : (localStorage.getItem('access_token') || localStorage.getItem('authToken'));
    const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char]);
    const formatDate = value => { const date = value ? new Date(String(value)) : null; return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : 'Not recorded'; };
    const empty = message => `<p class="text-muted mb-0">${escapeHtml(message)}</p>`;
    function showError(message) { document.getElementById('auditLoading')?.classList.add('is-hidden'); document.getElementById('auditError')?.classList.remove('is-hidden'); const node = document.getElementById('auditErrorText'); if (node) node.textContent = message; }
    function render(data) {
        const app = data.application || {}; const materials = data.materials || {};
        document.getElementById('auditTitle').textContent = `${app.job_title || 'Job application'}${app.company_name ? ` at ${app.company_name}` : ''}`;
        document.getElementById('auditSummary').textContent = `${app.portal || 'Portal not recorded'} | ${app.status || 'Unknown status'} | Created ${formatDate(app.created_at)}`;
        document.getElementById('auditMaterials').innerHTML = `<dt>Job-description snapshot</dt><dd>${materials.job_description_captured ? `Captured ${escapeHtml(formatDate(materials.job_description_captured_at))}` : 'Not available'}</dd><dt>Source URL</dt><dd>${materials.source_url ? `<a href="${escapeHtml(materials.source_url)}" target="_blank" rel="noopener noreferrer">Open source job</a>` : 'Not recorded'}</dd><dt>Workflow</dt><dd>${escapeHtml(materials.workflow_session_id || 'Not started')}</dd>`;
        const answers = Array.isArray(data.answers) ? data.answers : [];
        document.getElementById('auditAnswers').innerHTML = answers.length ? answers.map(answer => `<article class="border rounded p-3 mb-2"><div class="fw-semibold">${escapeHtml(answer.question)}</div><div class="mt-1">${escapeHtml(answer.answer)}</div><small class="text-muted">Source: ${escapeHtml(answer.answer_source)} | Submitted ${escapeHtml(formatDate(answer.submitted_at))}</small>${answer.review_reasons?.length ? `<div class="mt-2 text-warning small"><i class="fas fa-exclamation-triangle me-1"></i>Review: ${escapeHtml(answer.review_reasons.join(', '))}</div>` : ''}</article>`).join('') : empty('No portal submission answers have been recorded.');
        const holds = Array.isArray(data.holds) ? data.holds : [];
        document.getElementById('auditHolds').innerHTML = holds.length ? holds.map(hold => `<article class="border rounded p-3 mb-2"><div class="fw-semibold">${escapeHtml(hold.hold_code)}</div><div>${escapeHtml(hold.remediation)}</div>${hold.error_detail ? `<small class="text-muted">${escapeHtml(hold.error_detail)}</small>` : ''}<div class="small text-muted mt-1">${escapeHtml(hold.status)} | ${escapeHtml(formatDate(hold.created_at))}</div></article>`).join('') : empty('No holds or automation errors recorded.');
        const events = Array.isArray(data.events) ? data.events : [];
        document.getElementById('auditEvents').innerHTML = events.length ? `<ul class="mb-0">${events.map(event => `<li><strong>${escapeHtml(event.event_type)}</strong>${event.detail ? ` — ${escapeHtml(event.detail)}` : ''}<small class="text-muted ms-2">${escapeHtml(formatDate(event.created_at))}</small></li>`).join('')}</ul>` : empty('No lifecycle events recorded yet.');
        document.getElementById('auditLoading')?.classList.add('is-hidden'); document.getElementById('auditContent')?.classList.remove('is-hidden');
    }
    async function load() { if (!applicationId || !token()) { showError('Please sign in to view this audit log.'); return; } try { const response = await fetch(`${API_BASE}/applications/${encodeURIComponent(applicationId)}/audit`, { headers: { Authorization: `Bearer ${token()}` } }); const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.detail || 'Application not found.'); render(data); } catch (error) { showError(error instanceof Error ? error.message : 'Could not load audit log.'); } }
    document.addEventListener('DOMContentLoaded', load);
}());
