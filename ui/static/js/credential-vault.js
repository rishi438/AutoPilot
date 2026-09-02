(() => {
    'use strict';

    const API_BASE = (window.APP_CONFIG && window.APP_CONFIG.apiBase) || '/api/v1';
    const list = document.getElementById('credentialList');
    const revealModalElement = document.getElementById('revealCredentialModal');
    const revealModal = bootstrap.Modal.getOrCreateInstance(revealModalElement);
    let selectedCredentialId = null;
    let revealTimer = null;

    function authToken() {
        return (window.app && typeof window.app.getAuthToken === 'function')
            ? window.app.getAuthToken()
            : (localStorage.getItem('access_token') || localStorage.getItem('authToken'));
    }

    function authHeaders(json = false) {
        const headers = { Authorization: `Bearer ${authToken()}` };
        if (json) headers['Content-Type'] = 'application/json';
        return headers;
    }

    async function parseResponse(response) {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.message || data.detail || 'Credential vault request failed.');
        }
        return data;
    }

    function setStatus(elementId, message, error = false) {
        const element = document.getElementById(elementId);
        element.textContent = message;
        element.className = `status-message ${error ? 'text-danger' : 'text-success'}`;
    }

    function formMetadata(prefix) {
        return {
            portal_name: document.getElementById(`${prefix}PortalName`).value.trim(),
            portal_scope: document.getElementById(`${prefix}PortalScope`).value.trim().toLowerCase(),
            portal_login_url: document.getElementById(`${prefix}PortalUrl`).value.trim(),
            account_email: document.getElementById(`${prefix}Email`).value.trim().toLowerCase(),
        };
    }

    function hideRevealedPassword() {
        if (revealTimer) window.clearTimeout(revealTimer);
        revealTimer = null;
        document.getElementById('revealedPassword').textContent = '';
        document.getElementById('revealedSecret').classList.add('d-none');
        document.getElementById('reauthPassword').value = '';
    }

    function credentialRow(credential) {
        const row = document.createElement('article');
        row.className = 'credential-row';

        const meta = document.createElement('div');
        meta.className = 'credential-meta';
        const title = document.createElement('strong');
        title.textContent = credential.portal_name;
        const email = document.createElement('span');
        email.className = 'vault-muted';
        email.textContent = credential.account_email;
        const scope = document.createElement('span');
        scope.className = 'small vault-muted';
        scope.textContent = `${credential.portal_scope} · ${credential.status}`;
        meta.append(title, email, scope);

        const reveal = document.createElement('button');
        reveal.type = 'button';
        reveal.className = 'btn btn-outline-primary btn-sm';
        reveal.textContent = 'Reveal';
        reveal.addEventListener('click', () => {
            hideRevealedPassword();
            selectedCredentialId = credential.id;
            setStatus('revealStatus', '');
            revealModal.show();
        });
        row.append(meta, reveal);
        return row;
    }

    async function loadCredentials() {
        if (!authToken()) {
            window.location.assign('/auth/login');
            return;
        }
        list.textContent = 'Loading encrypted portal accounts…';
        try {
            const response = await fetch(`${API_BASE}/credential-vault`, {
                headers: authHeaders(),
                cache: 'no-store',
            });
            const data = await parseResponse(response);
            list.textContent = '';
            if (!data.credentials.length) {
                list.textContent = 'No portal credentials saved yet.';
                return;
            }
            data.credentials.forEach((credential) => list.appendChild(credentialRow(credential)));
        } catch (error) {
            list.textContent = error instanceof Error ? error.message : 'Could not load credentials.';
            list.className = 'text-danger';
        }
    }

    document.getElementById('generateCredentialForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        setStatus('generateStatus', 'Generating and encrypting…');
        try {
            const response = await fetch(`${API_BASE}/credential-vault/generated`, {
                method: 'POST',
                headers: authHeaders(true),
                body: JSON.stringify(formMetadata('generate')),
            });
            const data = await parseResponse(response);
            setStatus('generateStatus', data.created ? 'Encrypted credential created.' : 'Existing portal credential reused; no password was rotated.');
            await loadCredentials();
        } catch (error) {
            setStatus('generateStatus', error instanceof Error ? error.message : 'Could not create credential.', true);
        }
    });

    document.getElementById('existingCredentialForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        setStatus('existingStatus', 'Encrypting and saving…');
        const passwordInput = document.getElementById('existingPassword');
        try {
            const body = { ...formMetadata('existing'), password: passwordInput.value };
            const response = await fetch(`${API_BASE}/credential-vault/existing`, {
                method: 'PUT',
                headers: authHeaders(true),
                body: JSON.stringify(body),
            });
            await parseResponse(response);
            passwordInput.value = '';
            setStatus('existingStatus', 'Existing portal credential encrypted and saved.');
            await loadCredentials();
        } catch (error) {
            passwordInput.value = '';
            setStatus('existingStatus', error instanceof Error ? error.message : 'Could not save credential.', true);
        }
    });

    document.getElementById('revealCredentialForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!selectedCredentialId) return;
        const passwordInput = document.getElementById('reauthPassword');
        let currentPassword = passwordInput.value;
        hideRevealedPassword();
        setStatus('revealStatus', 'Verifying…');
        try {
            const response = await fetch(`${API_BASE}/credential-vault/${encodeURIComponent(selectedCredentialId)}/reveal`, {
                method: 'POST',
                headers: authHeaders(true),
                cache: 'no-store',
                body: JSON.stringify({ current_password: currentPassword }),
            });
            currentPassword = '';
            passwordInput.value = '';
            const data = await parseResponse(response);
            document.getElementById('revealedPassword').textContent = data.password;
            document.getElementById('revealedSecret').classList.remove('d-none');
            setStatus('revealStatus', 'Identity verified.');
            revealTimer = window.setTimeout(hideRevealedPassword, 30000);
        } catch (error) {
            currentPassword = '';
            passwordInput.value = '';
            setStatus('revealStatus', error instanceof Error ? error.message : 'Could not reveal credential.', true);
        }
    });

    revealModalElement.addEventListener('hidden.bs.modal', () => {
        selectedCredentialId = null;
        hideRevealedPassword();
    });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) hideRevealedPassword();
    });
    document.getElementById('refreshCredentials').addEventListener('click', loadCredentials);

    const query = new URLSearchParams(window.location.search);
    const prefill = {
        PortalName: query.get('portal_name'),
        PortalScope: query.get('portal_scope'),
        PortalUrl: query.get('portal_url'),
        Email: query.get('account_email'),
    };
    Object.entries(prefill).forEach(([suffix, value]) => {
        if (!value) return;
        ['generate', 'existing'].forEach((prefix) => {
            const element = document.getElementById(`${prefix}${suffix}`);
            if (element) element.value = value;
        });
    });

    loadCredentials();
})();
