// ==========================================
// 1. SUPABASE & API CONFIGURATION
// ==========================================
const SUPABASE_URL = "https://ddycdvmqonjvurxeaeht.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRkeWNkdm1xb25qdnVyeGVhZWh0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzk4Nzg3NTUsImV4cCI6MjA5NTQ1NDc1NX0.8GWqwtrn2duPQ6VKOEXoWnT_i2NQz1yBNDO2WW-UNbg";
const API_BASE = "https://defence-report-ai.onrender.com";

const supaClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
let sessionToken = null;
let isSignUpMode = false;
let currentReportId = null;

// ==========================================
// 2. AUTHENTICATION LOGIC
// ==========================================
async function checkSession() {
    const { data, error } = await supaClient.auth.getSession();
    if (data.session) {
        sessionToken = data.session.access_token;
        showApp();
        loadHistory();
    }
}
checkSession();

window.toggleAuthMode = function () {
    isSignUpMode = !isSignUpMode;
    document.getElementById('auth-action-btn').innerText = isSignUpMode ? "Register" : "Login";
    document.getElementById('auth-switch-btn').innerText = isSignUpMode ? "Existing Operative? Login" : "Request Access (Sign Up)";
    document.getElementById('auth-error').innerText = "";
}

window.handleAuth = async function () {
    const email = document.getElementById('email').value;
    const password = document.getElementById('password').value;
    const errorDiv = document.getElementById('auth-error');
    errorDiv.innerText = "Processing...";

    try {
        let result;
        if (isSignUpMode) {
            result = await supaClient.auth.signUp({ email, password });
            if (result.data.user && !result.data.session) {
                errorDiv.innerText = "Check email for verification link.";
                return;
            }
        } else {
            result = await supaClient.auth.signInWithPassword({ email, password });
        }

        if (result.error) throw result.error;

        sessionToken = result.data.session.access_token;
        showApp();
        loadHistory();
    } catch (err) {
        errorDiv.innerText = err.message;
    }
}

window.logout = async function () {
    await supaClient.auth.signOut();
    sessionToken = null;
    document.getElementById('app-view').style.display = 'none';
    document.getElementById('auth-view').style.display = 'flex';
}

function showApp() {
    document.getElementById('auth-view').style.display = 'none';
    document.getElementById('app-view').style.display = 'flex';
}

// ==========================================
// 3. THEME & UI LOGIC
// ==========================================
// window.toggleTheme = function () {
//     const body = document.body;
//     body.classList.toggle('dark-mode');
//     const btn = document.getElementById('theme-btn');
//     btn.innerText = body.classList.contains('dark-mode') ? 'DRK' : 'LGT';
// }


window.toggleTheme = function () {
    const body = document.body;
    body.classList.toggle('dark-mode');
    const icon = document.getElementById('theme-icon');

    if (body.classList.contains('dark-mode')) {
        // Change to Sun Icon
        icon.innerHTML = '<circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>';
    } else {
        // Change to Moon Icon
        icon.innerHTML = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>';
    }
}


const phases = [
    "CALCULATING TEMPORAL VECTORS...",
    "DEPLOYING REGIONAL SCRAPERS...",
    "EXTRACTING RAW DATA...",
    "70B BRAIN OUTLINING...",
    "8B INSTANT DRAFTING CHAPTERS..."
];
let tickerInterval;

function startTicker() {
    const ticker = document.getElementById('status-ticker');
    const text = document.getElementById('status-text');
    ticker.style.visibility = 'visible';
    let i = 0;
    text.innerText = phases[0];
    tickerInterval = setInterval(() => {
        i = (i + 1) % phases.length;
        text.innerText = phases[i];
    }, 3500);
}

function stopTicker() {
    clearInterval(tickerInterval);
    document.getElementById('status-ticker').style.visibility = 'hidden';
}

// ==========================================
// 4. API COMMUNICATION (FASTAPI)
// ==========================================
async function loadHistory() {
    try {
        const res = await fetch(`${API_BASE}/reports`, {
            headers: { 'Authorization': `Bearer ${sessionToken}` }
        });
        if (!res.ok) throw new Error("Failed to load history");
        const reports = await res.json();

        const list = document.getElementById('history-list');
        list.innerHTML = '';
        reports.forEach(r => {
            const btn = document.createElement('button');
            btn.className = 'history-item';
            const date = new Date(r.created_at).toLocaleDateString();
            btn.innerText = `[${date}] ${r.title}`;
            btn.onclick = () => fetchAndDisplayReport(r.id, btn);
            list.appendChild(btn);
        });
    } catch (err) {
        console.error(err);
    }
}

window.executeGeneration = async function () {
    const input = document.getElementById('topic-input');
    const topic = input.value.trim();
    if (!topic) return;

    const btn = document.getElementById('execute-btn');
    const viewport = document.getElementById('content-viewport');

    btn.disabled = true;
    viewport.classList.add('loading-overlay');
    startTicker();

    try {
        const res = await fetch(`${API_BASE}/generate`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${sessionToken}`
            },
            body: JSON.stringify({ topic })
        });

        if (!res.ok) throw new Error("API Error");
        const newReport = await res.json();

        input.value = '';
        loadHistory();
        renderReport(newReport);
    } catch (err) {
        alert("Execution failed. Check backend logs or token limits.");
    } finally {
        btn.disabled = false;
        viewport.classList.remove('loading-overlay');
        stopTicker();
    }
}

window.fetchAndDisplayReport = async function (id, btnElement) {
    document.querySelectorAll('.history-item').forEach(el => el.classList.remove('active'));
    if (btnElement) btnElement.classList.add('active');

    try {
        const res = await fetch(`${API_BASE}/reports/${id}`, {
            headers: { 'Authorization': `Bearer ${sessionToken}` }
        });
        const report = await res.json();
        renderReport(report);
    } catch (err) {
        alert("Failed to load report data.");
    }
}




function renderReport(report) {
    currentReportId = report.id;
    const viewport = document.getElementById('content-viewport');
    const dateStr = new Date(report.created_at || Date.now()).toLocaleString();

    // Build the collapsible sources HTML if sources exist
    let sourcesHtml = '';
    if (report.sources && report.sources.length > 0) {
        let links = report.sources.map(s => `<li><a href="${s}" target="_blank" style="color: var(--accent);">${s}</a></li>`).join('');
        sourcesHtml = `
        <details style="margin-top: 40px; margin-bottom: 20px; border: 1px solid var(--border); padding: 15px; cursor: pointer; font-family: 'Inter', sans-serif;">
            <summary style="font-family: 'JetBrains Mono'; font-weight: bold; text-transform: uppercase; outline: none;">[+] View Intelligence Sources</summary>
            <ul style="margin-top: 15px; font-size: 0.9rem; word-break: break-all; padding-left: 20px;">
                ${links}
            </ul>
        </details>`;
    }

    let html = `
        <h1>${report.title}</h1>
        <div class="meta-bar">
            <span>TARGET: ${report.topic || 'Archived'}</span>
            <span>WORDS: ${report.word_count || 'N/A'}</span>
            <span>TS: ${dateStr}</span>
        </div>
        ${marked.parse(report.content)}
        
        ${sourcesHtml}

        <div class="action-bar">
            <button class="action-btn" onclick="downloadDocx('${report.id}', \`${report.topic || 'Report'}\`)">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                    <polyline points="7 10 12 15 17 10"></polyline>
                    <line x1="12" y1="15" x2="12" y2="3"></line>
                </svg>
                DOWNLOAD .DOCX
            </button>
            <button class="action-btn" onclick="deleteReport('${report.id}')" style="color: var(--accent); border-color: var(--accent);">
                DELETE RECORD
            </button>
        </div>
    `;
    viewport.innerHTML = html;
    viewport.scrollTo(0, 0);
}

// window.downloadDocx = async function (id) {
//     try {
//         const res = await fetch(`${API_BASE}/reports/${id}/docx`, {
//             headers: { 'Authorization': `Bearer ${sessionToken}` }
//         });
//         if (!res.ok) throw new Error("Download failed");
//         const blob = await res.blob();
//         const url = window.URL.createObjectURL(blob);
//         const a = document.createElement('a');
//         a.href = url;
//         a.download = `OSINT_Report.docx`;
//         document.body.appendChild(a);
//         a.click();
//         a.remove();
//         window.URL.revokeObjectURL(url);
//     } catch (err) {
//         alert("Download failed.");
//     }
// }



window.downloadDocx = async function (id, topicText) {
    try {
        const res = await fetch(`${API_BASE}/reports/${id}/docx`, {
            headers: { 'Authorization': `Bearer ${sessionToken}` }
        });
        if (!res.ok) throw new Error("Download failed");
        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;

        // Generate clean dynamic filename from the first 3 words of the topic
        let cleanName = "OSINT";
        if (topicText && topicText !== 'Report') {
            cleanName = topicText.split(' ').slice(0, 3).join('_').replace(/[^a-zA-Z0-9_]/g, '').toLowerCase();
        }
        a.download = `${cleanName}_report.docx`;

        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
    } catch (err) {
        alert("Download failed.");
    }
}


window.deleteReport = async function (id) {
    if (!confirm("Erase this intelligence record permanently?")) return;
    try {
        await fetch(`${API_BASE}/reports/${id}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${sessionToken}` }
        });
        document.getElementById('content-viewport').innerHTML = '';
        loadHistory();
    } catch (err) {
        alert("Failed to delete record.");
    }
}