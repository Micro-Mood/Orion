// Orion — Vue 3 Application (VS Code Workbench)
// ==========================================

marked.setOptions({
    breaks: true,
    gfm: true,
    highlight: (code, lang) => {
        if (lang && hljs.getLanguage(lang)) {
            return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
    }
});

const { createApp, ref, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

createApp({
    setup() {
        // ---- State ----
        const sessions = ref([]);
        const activeSessionId = ref(null);
        const messages = ref([]);
        const inputText = ref('');
        const isConnected = ref(false);
        const isProcessing = ref(false);
        const sidebarVisible = ref(window.innerWidth > 768);
        const activeView = ref('chat');  // 'chat' | 'files' | 'settings'

        // 未读消息计数：其他会话中已完成但未查看的 AI 回复
        const unreadMap = ref({});  // session_id -> unread count

        // ---- Refs ----
        const chatArea = ref(null);
        const inputBox = ref(null);

        // ---- Computed ----
        const activeSessionTitle = computed(() => {
            const s = sessions.value.find(s => s.id === activeSessionId.value);
            return s ? (s.title || '新对话') : '';
        });

        const hasStreamingMessage = computed(() => {
            return messages.value.some(m => m.role === 'assistant' && m.streaming);
        });

        const canSend = computed(() => {
            return inputText.value.trim() && !isProcessing.value;
        });

        const unreadCount = computed(() => {
            return Object.values(unreadMap.value).reduce((a, b) => a + b, 0);
        });

        // ---- WebSocket ----
        let ws = null;
        let reconnectTimer = null;
        let reconnectDelay = 1000;
        const MAX_RECONNECT_DELAY = 30000;

        function connectWS() {
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const url = `${protocol}//${location.host}/ws`;
            ws = new WebSocket(url);

            ws.onopen = () => {
                isConnected.value = true;
                reconnectDelay = 1000;
                wsSend({ type: 'get_sessions' });
            };

            ws.onclose = () => {
                isConnected.value = false;
                scheduleReconnect();
            };

            ws.onerror = () => ws.close();

            ws.onmessage = (event) => {
                handleMessage(JSON.parse(event.data));
            };
        }

        function scheduleReconnect() {
            if (reconnectTimer) return;
            reconnectTimer = setTimeout(() => {
                reconnectTimer = null;
                reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
                connectWS();
            }, reconnectDelay);
        }

        function wsSend(data) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(data));
            }
        }

        // ---- Message Handling ----
        function handleMessage(data) {
            const handlers = {
                session_list: () => {
                    sessions.value = data.sessions;
                },
                session_created: () => {
                    sessions.value.unshift(data.session);
                    switchSession(data.session.id);
                },
                session_deleted: () => {
                    sessions.value = sessions.value.filter(s => s.id !== data.session_id);
                    if (activeSessionId.value === data.session_id) {
                        activeSessionId.value = sessions.value.length ? sessions.value[0].id : null;
                        messages.value = activeSessionId.value ? [] : [];
                        if (activeSessionId.value) loadMessages(activeSessionId.value);
                    }
                },
                session_messages: () => {
                    if (data.session_id === activeSessionId.value) {
                        messages.value = data.messages.map(m => ({
                            ...m,
                            tool_calls: (m.tool_calls || []).map(tc => ({ ...tc, expanded: false }))
                        }));
                        scrollToBottom();
                    }
                },
                message_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    messages.value.push({
                        id: data.message_id,
                        role: 'assistant',
                        content: '',
                        tool_calls: [],
                        streaming: true
                    });
                    isProcessing.value = true;
                    scrollToBottom();
                },
                message_delta: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findStreamingMessage();
                    if (msg) { msg.content += data.content; scrollToBottom(); }
                },
                message_end: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findMessage(data.message_id);
                    if (msg) {
                        msg.streaming = false;
                        if (data.content) msg.content = data.content;
                    }
                },
                tool_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        msg.tool_calls.push({
                            name: data.tool_name,
                            params: data.params,
                            status: 'running',
                            result: null,
                            duration: null,
                            expanded: false
                        });
                        scrollToBottom();
                    }
                },
                tool_end: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        const tc = msg.tool_calls.find(t => t.name === data.tool_name && t.status === 'running');
                        if (tc) {
                            tc.status = data.success ? 'success' : 'error';
                            tc.result = data.result;
                            tc.duration = data.duration;
                        }
                    }
                },
                done: () => {
                    if (data.session_id !== activeSessionId.value) {
                        // 其他会话有新回复，记为未读
                        unreadMap.value[data.session_id] = (unreadMap.value[data.session_id] || 0) + 1;
                        return;
                    }
                    isProcessing.value = false;
                    const msg = findStreamingMessage();
                    if (msg) msg.streaming = false;
                    updateSessionTitle(data.session_id);
                },
                ask: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    isProcessing.value = false;
                    const msg = findStreamingMessage();
                    if (msg) msg.streaming = false;
                },
                error: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    isProcessing.value = false;
                    const msg = findStreamingMessage();
                    if (msg) {
                        msg.streaming = false;
                        msg.content += `\n\n> ⚠️ ${data.message}`;
                    }
                },
                session_title_updated: () => {
                    const s = sessions.value.find(s => s.id === data.session_id);
                    if (s) s.title = data.title;
                }
            };

            const handler = handlers[data.type];
            if (handler) handler();
        }

        // ---- Helpers ----
        function findStreamingMessage() {
            return messages.value.findLast(m => m.role === 'assistant' && m.streaming);
        }

        function findMessage(id) {
            return messages.value.find(m => m.id === id);
        }

        function getLastAIMessage() {
            return messages.value.findLast(m => m.role === 'assistant');
        }

        // ---- Session Ops ----
        function createSession() {
            activeView.value = 'chat';
            wsSend({ type: 'create_session' });
        }

        function switchSession(id) {
            if (activeSessionId.value === id) return;
            activeSessionId.value = id;
            messages.value = [];
            isProcessing.value = false;
            // 清除该会话未读
            delete unreadMap.value[id];
            loadMessages(id);
            // 手机端选择会话后自动收起侧边栏
            if (window.innerWidth <= 768) sidebarVisible.value = false;
        }

        function loadMessages(sessionId) {
            wsSend({ type: 'get_messages', session_id: sessionId });
        }

        function deleteSession(id) {
            wsSend({ type: 'delete_session', session_id: id });
        }

        function updateSessionTitle(sessionId) {
            const s = sessions.value.find(s => s.id === sessionId);
            if (s && (!s.title || s.title === '新对话')) {
                const firstUserMsg = messages.value.find(m => m.role === 'user');
                if (firstUserMsg) {
                    const title = firstUserMsg.content.slice(0, 20) + (firstUserMsg.content.length > 20 ? '...' : '');
                    s.title = title;
                    wsSend({ type: 'update_session_title', session_id: sessionId, title });
                }
            }
        }

        // ---- Send ----
        function sendMessage() {
            const text = inputText.value.trim();
            if (!text || isProcessing.value) return;

            messages.value.push({
                id: 'user_' + Date.now(),
                role: 'user',
                content: text
            });

            wsSend({
                type: 'send_message',
                session_id: activeSessionId.value,
                content: text
            });

            inputText.value = '';
            isProcessing.value = true;
            scrollToBottom();
            resizeInput();
        }

        function handleKeydown(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
            // Ctrl+N → 新对话
            if (e.key === 'n' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                createSession();
            }
        }

        // ---- Render ----
        function renderMarkdown(text) {
            if (!text) return '';
            return marked.parse(text);
        }

        function formatJSON(obj) {
            if (typeof obj === 'string') return obj;
            return JSON.stringify(obj, null, 2);
        }

        function truncate(text, len) {
            if (typeof text !== 'string') text = JSON.stringify(text, null, 2);
            return text.length > len ? text.slice(0, len) + '...' : text;
        }

        // ---- Tool Display (Copilot 风格) ----
        function toolLabel(tc) {
            const p = tc.params || {};
            const name = tc.name;
            // 提取文件名（去掉长路径，只保留最后两级）
            function shortPath(fp) {
                if (!fp) return '文件';
                const parts = fp.replace(/\\/g, '/').split('/');
                return parts.length > 2 ? parts.slice(-2).join('/') : fp;
            }
            switch (name) {
                case 'read_file': {
                    let t = shortPath(p.path || p.filePath);
                    if (p.startLine) t += ` 行${p.startLine}` + (p.endLine ? `-${p.endLine}` : '');
                    return `读取 ${t}`;
                }
                case 'write_file':
                case 'create_file':
                    return `创建 ${shortPath(p.path || p.filePath)}`;
                case 'replace_string_in_file':
                case 'edit_file':
                case 'multi_replace_string_in_file': {
                    let t = shortPath(p.path || p.filePath);
                    if (p.startLine) t += ` 行${p.startLine}` + (p.endLine ? `-${p.endLine}` : '');
                    return `编辑 ${t}`;
                }
                case 'search_text':
                case 'grep_search':
                    return `搜索 "${p.pattern || p.query || ''}"`;
                case 'find_files':
                case 'file_search':
                    return `查找文件 "${p.pattern || p.query || ''}"`;
                case 'list_directory':
                case 'list_dir':
                    return `列出目录 ${shortPath(p.path)}`;
                case 'run_command':
                case 'run_in_terminal': {
                    const cmd = p.command || '';
                    return `运行 ${cmd.length > 40 ? cmd.slice(0, 40) + '…' : cmd}`;
                }
                case 'delete_file':
                    return `删除 ${shortPath(p.path || p.filePath)}`;
                case 'semantic_search':
                    return `语义搜索 "${(p.query || '').slice(0, 30)}"`;
                default:
                    return name;
            }
        }

        // 根据工具名返回 SVG 图标类名
        function toolIconClass(name) {
            const map = {
                read_file: 'icon-file',
                write_file: 'icon-new-file',
                create_file: 'icon-new-file',
                replace_string_in_file: 'icon-edit',
                edit_file: 'icon-edit',
                multi_replace_string_in_file: 'icon-edit',
                search_text: 'icon-search',
                grep_search: 'icon-search',
                find_files: 'icon-file-search',
                file_search: 'icon-file-search',
                list_directory: 'icon-folder',
                list_dir: 'icon-folder',
                run_command: 'icon-terminal',
                run_in_terminal: 'icon-terminal',
                delete_file: 'icon-trash',
                semantic_search: 'icon-search',
            };
            return map[name] || 'icon-gear';
        }

        // 判断结果是否可以作为代码/文件内容展示
        function isCodeResult(tc) {
            const codeTools = ['read_file', 'run_command', 'run_in_terminal'];
            return codeTools.includes(tc.name) && typeof tc.result === 'string' && tc.result.length > 0;
        }

        // ---- Avatar Generation (Boring Avatars 风格) ----
        function _avatarHash(str) {
            let h = 0;
            for (let i = 0; i < str.length; i++) {
                h = ((h << 5) - h) + str.charCodeAt(i);
                h = h & h;
            }
            return Math.abs(h);
        }

        function _avatarUnit(num, range, idx) {
            const v = num % range;
            return (Math.floor(num / Math.pow(10, idx || 0)) % 2 === 0) ? -v : v;
        }

        // Bean 风格用户头像（2色蓝色系）
        function genBeanAvatar(name) {
            const colors = ['#2c2c2c', '#e06c75'];
            const h = _avatarHash(name || 'user');
            const bg = colors[h % 2];
            const face = colors[(h + 1) % 2];
            const tx = _avatarUnit(h, 7, 1);
            const ty = _avatarUnit(h, 4, 2);
            const rot = _avatarUnit(h, 360, 3);
            const fRot = _avatarUnit(h, 8, 5);
            const mouthOpen = h % 2 === 0;
            const white = '#fff';
            const mouth = mouthOpen
                ? `<path d="M14.5 20.5a4 2.5 0 008 0" fill="${white}"/>`
                : `<path d="M15 21c1.5 1.5 5 1.5 6 0" stroke="${white}" fill="none" stroke-linecap="round"/>`;
            const svg = `<svg viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg">` +
                `<mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="36" height="36"><rect width="36" height="36" rx="72" fill="#fff"/></mask>` +
                `<g mask="url(#m)"><rect width="36" height="36" fill="${bg}"/>` +
                `<rect width="36" height="36" transform="translate(${tx} ${ty}) rotate(${rot} 18 18)" fill="${face}" rx="36"/>` +
                `<g transform="rotate(${fRot} 18 18)">` +
                `<ellipse cx="14" cy="15" rx="1.5" ry="2" fill="${white}"/>` +
                `<ellipse cx="22" cy="15" rx="1.5" ry="2" fill="${white}"/>` +
                mouth + `</g></g></svg>`;
            return 'data:image/svg+xml,' + encodeURIComponent(svg);
        }

        // Marble 风格 AI 头像（冰川蓝）
        function genMarbleAvatar(name) {
            const colors = ['#0d4a7a', '#74b9ff', '#a8d8ea'];
            const h = _avatarHash(name || 'orion');
            const c0 = colors[0], c1 = colors[1], c2 = colors[2];
            const rot1 = h % 360;
            const rot2 = (h * 7 + 123) % 360;
            const rot3 = (h * 13 + 67) % 360;
            const cx1 = 30 + (h % 40), cy1 = 20 + ((h >> 3) % 40);
            const cx2 = 10 + ((h >> 5) % 60), cy2 = 50 + ((h >> 7) % 30);
            const cx3 = 50 + ((h >> 9) % 30), cy3 = 10 + ((h >> 2) % 60);
            const svg = `<svg viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg">` +
                `<mask id="mm" maskUnits="userSpaceOnUse" x="0" y="0" width="80" height="80">` +
                `<rect width="80" height="80" rx="160" fill="#fff"/></mask>` +
                `<g mask="url(#mm)">` +
                `<rect width="80" height="80" fill="${c0}"/>` +
                `<g transform="rotate(${rot1} 40 40)" style="mix-blend-mode:overlay">` +
                `<ellipse cx="${cx1}" cy="${cy1}" rx="50" ry="30" fill="${c1}" opacity="0.7"/></g>` +
                `<g transform="rotate(${rot2} 40 40)" style="mix-blend-mode:soft-light">` +
                `<ellipse cx="${cx2}" cy="${cy2}" rx="35" ry="55" fill="${c2}" opacity="0.8"/></g>` +
                `<g transform="rotate(${rot3} 40 40)" style="mix-blend-mode:overlay">` +
                `<circle cx="${cx3}" cy="${cy3}" r="28" fill="${c1}" opacity="0.4"/></g>` +
                `<g style="mix-blend-mode:soft-light" opacity="0.5">` +
                `<path d="M${10+(h%20)} ${5+((h>>4)%30)} Q${40+((h>>6)%20)} ${20+((h>>1)%30)} ${70-((h>>3)%15)} ${60+((h>>8)%15)}" stroke="${c2}" stroke-width="3" fill="none" opacity="0.6"/>` +
                `<path d="M${5+((h>>2)%25)} ${50+((h>>5)%20)} Q${35+((h>>7)%20)} ${30+((h>>4)%25)} ${65+((h>>1)%10)} ${10+((h>>6)%20)}" stroke="${c1}" stroke-width="2" fill="none" opacity="0.5"/>` +
                `</g></g></svg>`;
            return 'data:image/svg+xml,' + encodeURIComponent(svg);
        }

        const userAvatar = computed(() => genBeanAvatar(activeSessionId.value || 'default'));
        const aiAvatar = computed(() => genMarbleAvatar('orion'));

        function formatTime(ts) {
            if (!ts) return '';
            const d = new Date(ts);
            const now = new Date();
            if (d.toDateString() === now.toDateString()) {
                return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
            }
            return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
        }

        // ---- Scroll ----
        function scrollToBottom() {
            nextTick(() => {
                if (chatArea.value) {
                    chatArea.value.scrollTop = chatArea.value.scrollHeight;
                }
            });
        }

        // ---- Input auto-resize ----
        function resizeInput() {
            nextTick(() => {
                const el = inputBox.value;
                if (el) {
                    el.style.height = 'auto';
                    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
                }
            });
        }

        watch(inputText, resizeInput);

        // ---- Sidebar resize ----
        let resizing = false;

        function startResize(e) {
            resizing = true;
            const sash = e.target;
            sash.classList.add('active');
            const startX = e.clientX;
            const startWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'));

            function onMove(e2) {
                const delta = e2.clientX - startX;
                const newWidth = Math.max(150, Math.min(500, startWidth + delta));
                document.documentElement.style.setProperty('--sidebar-width', newWidth + 'px');
            }

            function onUp() {
                resizing = false;
                sash.classList.remove('active');
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

        // ---- Global keyboard shortcuts ----
        function handleGlobalKeydown(e) {
            if (e.key === 'b' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                sidebarVisible.value = !sidebarVisible.value;
            }
            if (e.key === 'n' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                createSession();
            }
        }

        // ---- Lifecycle ----
        onMounted(() => {
            connectWS();
            document.addEventListener('keydown', handleGlobalKeydown);
        });

        onUnmounted(() => {
            if (ws) ws.close();
            if (reconnectTimer) clearTimeout(reconnectTimer);
            document.removeEventListener('keydown', handleGlobalKeydown);
        });

        return {
            sessions, activeSessionId, messages, inputText,
            isConnected, isProcessing, sidebarVisible, activeView,
            activeSessionTitle, hasStreamingMessage, canSend, unreadCount,
            userAvatar, aiAvatar,
            chatArea, inputBox,
            createSession, switchSession, deleteSession, sendMessage,
            handleKeydown, startResize,
            renderMarkdown, formatJSON, truncate, formatTime,
            toolLabel, toolIconClass, isCodeResult
        };
    }
}).mount('#app');
