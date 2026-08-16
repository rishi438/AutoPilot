(function () {
    'use strict';
    const API_BASE = (window.APP_CONFIG && window.APP_CONFIG.apiBase) || '/api/v1';
    const root = document.querySelector('[data-application-id]');
    const applicationId = root ? String(root.dataset.applicationId || '') : '';
    const token = () => window.app && typeof window.app.getAuthToken === 'function' ? window.app.getAuthToken() : (localStorage.getItem('access_token') || localStorage.getItem('authToken'));
    function showError(message) { document.getElementById('savedJobLoading')?.classList.add('is-hidden'); document.getElementById('savedJobError')?.classList.remove('is-hidden'); const text = document.getElementById('savedJobErrorText'); if (text) text.textContent = message; }
    function formatDate(value) { const date = value ? new Date(String(value)) : null; return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : 'Unknown'; }
    async function confirmAndAnalyze(button, jobTitle, companyName) {
        if (button.disabled) return;
        if (typeof window.showConfirm !== 'function') {
            showError('The analysis confirmation dialog is unavailable. Please refresh and try again.');
            return;
        }
        button.disabled = true;
        try {
            const confirmed = await window.showConfirm({
                title: 'Start job analysis?',
                message: `Analyze ${jobTitle} at ${companyName} using its saved job description? This starts AI processing and may take a minute.`,
                confirmText: 'Start analysis',
                cancelText: 'Cancel',
                type: 'primary'
            });
            if (confirmed) await analyze(button);
        } finally {
            if (button.textContent !== 'Starting analysis...') button.disabled = false;
        }
    }
    async function analyze(button) {
        button.disabled = true; button.textContent = 'Starting analysis...';
        try {
            const response = await fetch(`${API_BASE}/workflow/analyze-saved-job/${encodeURIComponent(applicationId)}`, { method: 'POST', headers: { Authorization: `Bearer ${token()}` } });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || data.message || 'Could not start analysis.');
            window.location.href = `/dashboard/application/${encodeURIComponent(data.session_id)}`;
        } catch (error) { button.disabled = false; button.innerHTML = '<i class="fas fa-search me-2"></i>Analyze job'; showError(error instanceof Error ? error.message : 'Could not start analysis.'); }
    }
    async function load() {
        if (!applicationId || !token()) { showError('Please sign in to view this saved job.'); return; }
        try {
            const response = await fetch(`${API_BASE}/applications/${encodeURIComponent(applicationId)}/job-detail`, { headers: { Authorization: `Bearer ${token()}` } });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Saved job not found.');
            document.getElementById('savedJobTitle').textContent = data.job_title || 'Job Application'; document.getElementById('savedJobCompany').textContent = data.company_name || 'Unknown company'; document.getElementById('savedJobDescription').textContent = data.job_description;
            const auditLink = document.getElementById('savedJobAuditLink'); if (auditLink) auditLink.href = `/dashboard/application/${encodeURIComponent(applicationId)}/audit`;
            const metadata = document.getElementById('savedJobMetadata');
            if (metadata) { const source = data.job_url || data.external_ats_url; metadata.textContent = `${data.portal || 'Portal not recorded'} | Captured ${formatDate(data.job_description_captured_at)}`; if (source) { const link = document.createElement('a'); link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open source job'; metadata.append(' | ', link); } }
            const button = document.getElementById('analyzeSavedJob');
            if (data.workflow_session_id) { button.textContent = 'View analysis'; button.addEventListener('click', () => { window.location.href = `/dashboard/application/${encodeURIComponent(data.workflow_session_id)}`; }); } else { button.addEventListener('click', () => confirmAndAnalyze(button, data.job_title || 'this job', data.company_name || 'the company')); }
            document.getElementById('savedJobLoading')?.classList.add('is-hidden'); document.getElementById('savedJobContent')?.classList.remove('is-hidden');
        } catch (error) { showError(error instanceof Error ? error.message : 'Could not load saved job.'); }
    }
    document.addEventListener('DOMContentLoaded', load);
}());
