// Orion — Vue 3 Application (VS Code Workbench)
// ==========================================

// Auto-detect base path for sub-path deployment (e.g. /orion/)
const BASE = location.pathname.replace(/\/[^/]*$/, '') || '';

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

const { createApp, ref, reactive, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

createApp({
    setup() {
        // ==================== 核心状态 ====================
        const sessions = ref([]);
        const activeSessionId = ref(null);
        const messages = ref([]);
        const inputText = ref('');
        const isConnected = ref(false);
        const isProcessing = ref(false);
        const askOptionsMap = ref({});
        const askOptions = computed(() => askOptionsMap.value[activeSessionId.value] || []);
        const sidebarVisible = ref(window.innerWidth > 768);
        const sidebarView = ref('chat');  // 'chat' | 'files' | 'settings' — 只控制侧边栏内容
        const settingsOpen = ref(false); // 是否显示设置页（独立于侧边栏）
        const currentModel = ref('');
        const isMobile = ref(window.innerWidth <= 768);

        // ==================== 认证状态 ====================
        const loggedIn = ref(false);
        const needsSetup = ref(false);
        const authToken = ref(localStorage.getItem('orion_token') || '');
        const loginError = ref('');
        const loginLoading = ref(false);
        const loginPassword = ref('');

        // 监听窗口大小
        function _onResize() { isMobile.value = window.innerWidth <= 768; }
        window.addEventListener('resize', _onResize);

        // 未读消息计数
        const unreadMap = ref({});

        // ==================== 设置状态 ====================
        const settingsTab = ref('llm');
        const showApiKey = ref(false);
        const configSaving = ref(false);
        const configSaveMsg = ref('');
        const configSaveSuccess = ref(false);
        const effectiveCwd = ref('');
        const testingLLM = ref(false);

        // ==================== 文件浏览状态 ====================
        const fileTree = ref([]);          // 根级条目，每个节点 { name, type, size, path, depth, expanded, loaded, loading, children }
        const fileRootPath = ref('');      // 根目录路径
        const _pendingNodes = {};          // path → node，支持多目录同时加载
        let _fileTreeDirty = false;        // 文件系统变化时标记，切换视图时刷新

        // 计算属性: 将树展平为可渲染列表 (只输出可见节点)
        const flatFileList = computed(() => {
            const result = [];
            function walk(nodes) {
                for (const n of nodes) {
                    result.push(n);
                    if (n.type === 'directory' && n.expanded && n.children.length) {
                        walk(n.children);
                    }
                }
            }
            walk(fileTree.value);
            return result;
        });

        const openFilePath = ref('');
        const openFileContent = ref('');
        const openFileName = computed(() => {
            if (!openFilePath.value) return '';
            return openFilePath.value.replace(/\\/g, '/').split('/').pop();
        });
        const fileLoading = ref(false);
        const fileError = ref('');
        const testingAxon = ref(false);
        const restartingAxon = ref(false);
        const llmTestResult = ref(null);
        const axonTestResult = ref(null);

        const configForm = reactive({
            llm: {
                api_key: '',
                base_url: '',
                models_str: '',
                temperature: 0.7,
                timeout: 120,
                max_retries: 3,
            },
            axon: {
                host: '127.0.0.1',
                port: 9100,
                connect_timeout: 5.0,
                call_timeout: 60.0,
            },
            engine: {
                working_directory: '',
                max_history: 20,
                max_iterations: 30,
            }
        });

        // ==================== Refs ====================
        const chatArea = ref(null);
        const inputBox = ref(null);

        // ==================== Computed ====================
        const activeSessionTitle = computed(() => {
            const s = sessions.value.find(s => s.id === activeSessionId.value);
            return s ? (s.title || '新对话') : '';
        });

        const hasStreamingMessage = computed(() => {
            return messages.value.some(m => m.role === 'assistant' && m.streaming);
        });

        const canSend = computed(() => {
            return inputText.value.trim() && !isProcessing.value && activeSessionId.value;
        });

        const unreadCount = computed(() => {
            return Object.values(unreadMap.value).reduce((a, b) => a + b, 0);
        });

        // ==================== WebSocket ====================
        let ws = null;
        let reconnectTimer = null;
        let reconnectDelay = 1000;
        const MAX_RECONNECT_DELAY = 30000;
        let _pendingAfterSave = null;  // 配置保存后执行的回调

        function connectWS() {
            const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            const url = `${protocol}//${location.host}${BASE}/ws`;
            ws = new WebSocket(url);

            ws.onopen = () => {
                // 首条消息认证（避免 token 暴露在 URL 查询参数中）
                ws.send(JSON.stringify({ type: 'auth', token: authToken.value }));
            };

            ws.onclose = (e) => {
                isConnected.value = false;
                if (e.code === 4001) {
                    // 认证失败，不重连
                    loggedIn.value = false;
                    authToken.value = '';
                    localStorage.removeItem('orion_token');
                    return;
                }
                scheduleReconnect();
            };

            ws.onerror = () => ws.close();

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    // 认证握手响应
                    if (data.type === 'auth_ok') {
                        isConnected.value = true;
                        reconnectDelay = 1000;
                        wsSend({ type: 'get_sessions' });
                        if (activeSessionId.value) {
                            wsSend({ type: 'get_messages', session_id: activeSessionId.value });
                        }
                        return;
                    }
                    if (data.type === 'auth_fail') {
                        loggedIn.value = false;
                        authToken.value = '';
                        localStorage.removeItem('orion_token');
                        return;
                    }
                    handleMessage(data);
                } catch (e) {
                    console.error('WS message parse error:', e);
                }
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

        // ==================== 消息处理 ====================
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
                        messages.value = [];
                        if (activeSessionId.value) loadMessages(activeSessionId.value);
                    }
                },

                session_messages: () => {
                    if (data.session_id === activeSessionId.value) {
                        messages.value = data.messages.map(m => ({
                            ...m,
                            segments: (m.segments || []).map(seg => {
                                if (seg.type === 'tool') {
                                    return { ...seg, expanded: false };
                                }
                                return { ...seg };
                            }),
                        }));
                        if (data.pending_options && data.pending_options.length) {
                            askOptionsMap.value[data.session_id] = data.pending_options;
                        }
                        scrollToBottom();
                    }
                },

                message_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    messages.value.push({
                        id: data.message_id,
                        role: 'assistant',
                        segments: [],
                        streaming: true
                    });
                    isProcessing.value = true;
                    scrollToBottom();
                },

                message_delta: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findStreamingMessage();
                    if (msg) {
                        // 追加到最后一个 text segment，或创建新的
                        const segs = msg.segments;
                        if (segs.length > 0 && segs[segs.length - 1].type === 'text') {
                            segs[segs.length - 1].content += data.content;
                        } else {
                            segs.push({ type: 'text', content: data.content });
                        }
                        scrollToBottom();
                    }
                },

                message_end: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findMessage(data.message_id);
                    if (msg) {
                        msg.streaming = false;
                        // 如果服务端发了最终文本且当前无文本 segment，补上
                        if (data.content) {
                            const hasText = msg.segments.some(s => s.type === 'text');
                            if (!hasText) {
                                msg.segments.push({ type: 'text', content: data.content });
                            }
                        }
                    }
                },

                tool_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        msg.segments.push({
                            type: 'tool',
                            id: data.tool_id || '',
                            name: data.tool_name,
                            params: data.params,
                            status: 'running',
                            result: null,
                            duration: null,
                            expanded: false,
                        });
                        scrollToBottom();
                    }
                },

                tool_end: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        // 优先按 tool_id 匹配，其次按 name + running 匹配
                        let toolSeg = null;
                        if (data.tool_id) {
                            toolSeg = msg.segments.findLast(
                                s => s.type === 'tool' && s.id === data.tool_id
                            );
                        }
                        if (!toolSeg) {
                            toolSeg = msg.segments.findLast(
                                s => s.type === 'tool' && s.name === data.tool_name && s.status === 'running'
                            );
                        }
                        if (toolSeg) {
                            toolSeg.status = data.success ? 'success' : 'error';
                            toolSeg.result = data.result;
                            toolSeg.duration = data.duration;
                        }
                    }
                },

                done: () => {
                    if (data.session_id !== activeSessionId.value) {
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
                    askOptionsMap.value[data.session_id] = Array.isArray(data.options) ? data.options : [];
                },

                error: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    isProcessing.value = false;
                    const msg = findStreamingMessage();
                    if (msg) {
                        msg.streaming = false;
                        if (data.message) {
                            // 追加到最后一个 text segment 或创建新的
                            const errText = `\n\n> [!] ${data.message}`;
                            const segs = msg.segments;
                            if (segs.length > 0 && segs[segs.length - 1].type === 'text') {
                                segs[segs.length - 1].content += errText;
                            } else {
                                segs.push({ type: 'text', content: errText });
                            }
                        }
                    }
                },

                session_title_updated: () => {
                    const s = sessions.value.find(s => s.id === data.session_id);
                    if (s) s.title = data.title;
                },

                model_info: () => {
                    if (data.model) {
                        currentModel.value = data.model;
                    }
                },

                // ---- 设置相关 ----
                config_data: () => {
                    const cfg = data.config;
                    if (!cfg) return;
                    effectiveCwd.value = cfg.effective_cwd || '';
                    // LLM
                    if (cfg.llm) {
                        configForm.llm.api_key = cfg.llm.api_key || '';
                        configForm.llm.base_url = cfg.llm.base_url || '';
                        configForm.llm.models_str = Array.isArray(cfg.llm.models)
                            ? cfg.llm.models.join(', ')
                            : '';
                        configForm.llm.temperature = cfg.llm.temperature ?? 0.7;
                        configForm.llm.timeout = cfg.llm.timeout ?? 120;
                        configForm.llm.max_retries = cfg.llm.max_retries ?? 3;
                    }
                    // Axon
                    if (cfg.axon) {
                        configForm.axon.host = cfg.axon.host || '127.0.0.1';
                        configForm.axon.port = cfg.axon.port ?? 9100;
                        configForm.axon.connect_timeout = cfg.axon.connect_timeout ?? 5.0;
                        configForm.axon.call_timeout = cfg.axon.call_timeout ?? 60.0;
                    }
                    // Engine
                    if (cfg.engine) {
                        configForm.engine.working_directory = cfg.engine.working_directory || '';
                        configForm.engine.max_history = cfg.engine.max_history ?? 20;
                        configForm.engine.max_iterations = cfg.engine.max_iterations ?? 30;
                    }
                },

                config_saved: () => {
                    configSaving.value = false;
                    configSaveMsg.value = data.message || '已保存';
                    configSaveSuccess.value = true;
                    // 更新表单 (服务端返回的最新值)
                    if (data.config) {
                        handleMessage({ type: 'config_data', config: data.config });
                    }
                    clearSaveMsg();
                    // 触发等待配置保存后的操作
                    if (_pendingAfterSave) {
                        const fn = _pendingAfterSave;
                        _pendingAfterSave = null;
                        fn();
                    }
                },

                test_result: () => {
                    const result = { success: data.success, message: data.message };
                    if (data.target === 'llm') {
                        testingLLM.value = false;
                        llmTestResult.value = result;
                        setTimeout(() => { llmTestResult.value = null; }, 8000);
                    } else if (data.target === 'axon') {
                        testingAxon.value = false;
                        restartingAxon.value = false;
                        axonTestResult.value = result;
                        setTimeout(() => { axonTestResult.value = null; }, 8000);
                    }
                },

                // ---- 文件浏览 ----
                file_list: () => {
                    fileLoading.value = false;
                    const parentPath = (data.path || '').replace(/\\/g, '/');
                    if (data.error) {
                        fileError.value = data.error;
                        const node = _pendingNodes[parentPath];
                        if (node) { node.loading = false; }
                        delete _pendingNodes[parentPath];
                        return;
                    }
                    fileError.value = '';
                    const sep = '/';

                    // 排序: 目录在前, 文件在后
                    const raw = (data.entries || []).slice();
                    raw.sort((a, b) => {
                        const aDir = a.type === 'directory' ? 0 : 1;
                        const bDir = b.type === 'directory' ? 0 : 1;
                        if (aDir !== bDir) return aDir - bDir;
                        return (a.name || '').localeCompare(b.name || '');
                    });

                    const node = _pendingNodes[parentPath];
                    if (node) {
                        // 填充某个目录的子节点
                        node.children = raw.map(e => ({
                            name: e.name,
                            type: e.type,
                            size: e.size,
                            path: parentPath + sep + e.name,
                            depth: node.depth + 1,
                            expanded: false,
                            loaded: false,
                            loading: false,
                            children: [],
                        }));
                        node.loaded = true;
                        node.loading = false;
                        delete _pendingNodes[parentPath];
                    } else {
                        // 根级加载
                        fileRootPath.value = parentPath;
                        fileTree.value = raw.map(e => ({
                            name: e.name,
                            type: e.type,
                            size: e.size,
                            path: parentPath + sep + e.name,
                            depth: 0,
                            expanded: false,
                            loaded: false,
                            loading: false,
                            children: [],
                        }));
                    }
                },

                file_content: () => {
                    fileLoading.value = false;
                    if (data.error) {
                        // 文件不存在 → 关闭预览（删除/移动场景）
                        if (openFilePath.value === data.path) {
                            openFilePath.value = '';
                            openFileContent.value = '';
                        }
                    } else {
                        openFilePath.value = data.path || '';
                        openFileContent.value = data.content || '';
                    }
                },

                fs_changed: () => {
                    // 文件系统变化 — 刷新目录树
                    if (sidebarView.value === 'files') {
                        loadFileRoot();
                    } else {
                        _fileTreeDirty = true;
                    }
                    // 如果当前打开的文件被修改，重新加载内容
                    if (openFilePath.value) {
                        const changed = data.paths || [];
                        const norm = p => p.replace(/\\/g, '/');
                        if (changed.some(p => norm(p) === norm(openFilePath.value))) {
                            wsSend({ type: 'read_file_content', path: openFilePath.value });
                        }
                    }
                },
            };

            const handler = handlers[data.type];
            if (handler) handler();
        }

        // ==================== 辅助函数 ====================
        function findStreamingMessage() {
            return messages.value.findLast(m => m.role === 'assistant' && m.streaming);
        }

        function findMessage(id) {
            return messages.value.find(m => m.id === id);
        }

        function getLastAIMessage() {
            return messages.value.findLast(m => m.role === 'assistant');
        }

        function getTextContent(msg) {
            if (!msg || !msg.segments) return '';
            return msg.segments
                .filter(s => s.type === 'text')
                .map(s => s.content)
                .join('');
        }

        // ==================== 会话操作 ====================
        function createSession() {
            sidebarView.value = 'chat';
            settingsOpen.value = false;
            wsSend({ type: 'create_session' });
        }

        function switchSession(id) {
            if (isMobile.value) sidebarVisible.value = false;
            if (activeSessionId.value === id) return;
            activeSessionId.value = id;
            messages.value = [];
            isProcessing.value = false;
            settingsOpen.value = false;
            delete unreadMap.value[id];
            loadMessages(id);
        }

        function loadMessages(sessionId) {
            wsSend({ type: 'get_messages', session_id: sessionId });
        }

        function deleteSession(id) {
            const s = sessions.value.find(s => s.id === id);
            const title = s?.title || '新对话';
            if (!confirm(`确认删除会话「${title}」？`)) return;
            wsSend({ type: 'delete_session', session_id: id });
        }

        function updateSessionTitle(sessionId) {
            const s = sessions.value.find(s => s.id === sessionId);
            if (s && (!s.title || s.title === '新对话')) {
                const firstUserMsg = messages.value.find(m => m.role === 'user');
                if (firstUserMsg) {
                    const text = getTextContent(firstUserMsg);
                    const title = text.slice(0, 20) + (text.length > 20 ? '...' : '');
                    s.title = title;
                    wsSend({ type: 'update_session_title', session_id: sessionId, title });
                }
            }
        }

        // ==================== 发送消息 ====================
        function sendMessage() {
            const text = inputText.value.trim();
            // Fix B4: 检查 activeSessionId
            if (!text || isProcessing.value || !activeSessionId.value) return;

            delete askOptionsMap.value[activeSessionId.value];
            messages.value.push({
                id: 'user_' + Date.now(),
                role: 'user',
                segments: [{ type: 'text', content: text }],
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

        function cancelProcessing() {
            if (!activeSessionId.value) return;
            wsSend({ type: 'cancel', session_id: activeSessionId.value });
        }

        function selectOption(option) {
            inputText.value = option;
            sendMessage();
        }

        function handleKeydown(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
            if (e.key === 'n' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                createSession();
            }
        }

        // ==================== 文件浏览 ====================
        function loadFileRoot() {
            fileLoading.value = true;
            fileError.value = '';
            // 清空所有 pending（根级重载）
            for (const k in _pendingNodes) delete _pendingNodes[k];
            wsSend({ type: 'list_files', path: '' });
        }

        function toggleFolder(node) {
            if (!node || node.type !== 'directory') return;
            if (node.expanded) {
                node.expanded = false;
                return;
            }
            node.expanded = true;
            if (!node.loaded) {
                node.loading = true;
                _pendingNodes[node.path] = node;
                wsSend({ type: 'list_files', path: node.path });
            }
        }

        function openFileEntry(node) {
            if (node.type === 'directory') {
                toggleFolder(node);
            } else {
                fileLoading.value = true;
                fileError.value = '';
                wsSend({ type: 'read_file_content', path: node.path });
                if (isMobile.value) sidebarVisible.value = false;
            }
        }

        function closeFilePreview() {
            openFilePath.value = '';
            openFileContent.value = '';
        }

        function mobileBackToFiles() {
            openFilePath.value = '';
            openFileContent.value = '';
            sidebarView.value = 'files';
            sidebarVisible.value = true;
        }

        function getFileExtension(name) {
            const dot = name.lastIndexOf('.');
            return dot > 0 ? name.substring(dot + 1).toLowerCase() : '';
        }

        function getFileLanguage(name) {
            const ext = getFileExtension(name);
            const map = {
                js: 'javascript', jsx: 'javascript', ts: 'typescript', tsx: 'typescript',
                py: 'python', rb: 'ruby', rs: 'rust', go: 'go', java: 'java',
                c: 'c', cpp: 'cpp', h: 'c', hpp: 'cpp',
                css: 'css', scss: 'scss', less: 'less',
                html: 'html', htm: 'html', xml: 'xml', svg: 'xml',
                json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml',
                md: 'markdown', sh: 'bash', bat: 'dos', ps1: 'powershell',
                sql: 'sql', dockerfile: 'dockerfile',
            };
            return map[ext] || '';
        }

        const openFileHtml = computed(() => {
            const code = openFileContent.value;
            if (!code) return '';
            const lang = getFileLanguage(openFileName.value);
            try {
                if (lang && hljs.getLanguage(lang)) {
                    return hljs.highlight(code, { language: lang }).value;
                }
            } catch (_) {}
            // 无法识别语言时直接返回转义文本，不用 highlightAuto（大文件会卡死）
            const d = document.createElement('span');
            d.textContent = code;
            return d.innerHTML;
        });

        const openFileLineCount = computed(() => {
            const code = openFileContent.value;
            if (!code) return 0;
            return code.split('\n').length;
        });

        function formatFileSize(bytes) {
            if (bytes == null) return '';
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }

        // ==================== 设置操作 ====================
        function switchToSettings() {
            sidebarView.value = 'settings';
            settingsOpen.value = true;
            if (!isMobile.value) sidebarVisible.value = true;
            wsSend({ type: 'get_config' });
        }

        function closeSettings() {
            settingsOpen.value = false;
            sidebarView.value = 'chat';
        }

        function toggleSidebarView(view) {
            if (sidebarVisible.value && sidebarView.value === view) {
                sidebarVisible.value = false;
            } else {
                if (view === 'settings') {
                    switchToSettings();
                } else {
                    sidebarView.value = view;
                    sidebarVisible.value = true;
                }
            }
        }

        function saveConfig(sections = null) {
            configSaving.value = true;
            configSaveMsg.value = '';

            const wanted = Array.isArray(sections) && sections.length
                ? new Set(sections)
                : new Set([settingsTab.value]);

            // 把 models_str 解析回数组
            const modelsArr = configForm.llm.models_str
                .split(',')
                .map(s => s.trim())
                .filter(s => s.length > 0);

            const payload = {};
            if (wanted.has('llm')) {
                payload.llm = {
                    api_key: configForm.llm.api_key,
                    base_url: configForm.llm.base_url,
                    models: modelsArr,
                    temperature: configForm.llm.temperature,
                    timeout: configForm.llm.timeout,
                    max_retries: configForm.llm.max_retries,
                };
            }
            if (wanted.has('axon')) {
                payload.axon = {
                    host: configForm.axon.host,
                    port: configForm.axon.port,
                    connect_timeout: configForm.axon.connect_timeout,
                    call_timeout: configForm.axon.call_timeout,
                };
            }
            if (wanted.has('engine')) {
                payload.engine = {
                    working_directory: configForm.engine.working_directory,
                    max_history: configForm.engine.max_history,
                    max_iterations: configForm.engine.max_iterations,
                };
            }

            wsSend({
                type: 'save_config',
                config: payload,
            });

            // 超时保底
            setTimeout(() => {
                if (configSaving.value) {
                    configSaving.value = false;
                    configSaveMsg.value = '保存超时，请检查连接';
                    configSaveSuccess.value = false;
                    clearSaveMsg();
                }
            }, 10000);
        }

        function clearSaveMsg() {
            setTimeout(() => { configSaveMsg.value = ''; }, 5000);
        }

        function testLLM() {
            testingLLM.value = true;
            llmTestResult.value = null;
            // 先保存，等 config_saved 回调后再测试
            saveConfig(['llm']);
            _pendingAfterSave = () => wsSend({ type: 'test_llm' });
            // 超时保底
            setTimeout(() => {
                if (testingLLM.value) {
                    _pendingAfterSave = null;
                    testingLLM.value = false;
                    llmTestResult.value = { success: false, message: '测试超时' };
                    setTimeout(() => { llmTestResult.value = null; }, 5000);
                }
            }, 30000);
        }

        function testAxon() {
            testingAxon.value = true;
            axonTestResult.value = null;
            saveConfig(['axon']);
            _pendingAfterSave = () => wsSend({ type: 'test_axon' });
            setTimeout(() => {
                if (testingAxon.value) {
                    _pendingAfterSave = null;
                    testingAxon.value = false;
                    axonTestResult.value = { success: false, message: '测试超时' };
                    setTimeout(() => { axonTestResult.value = null; }, 5000);
                }
            }, 15000);
        }

        function restartAxon() {
            restartingAxon.value = true;
            axonTestResult.value = null;
            saveConfig(['axon']);
            _pendingAfterSave = () => wsSend({ type: 'restart_axon' });
            setTimeout(() => {
                if (restartingAxon.value) {
                    _pendingAfterSave = null;
                    restartingAxon.value = false;
                    axonTestResult.value = { success: false, message: '重启超时' };
                    setTimeout(() => { axonTestResult.value = null; }, 5000);
                }
            }, 20000);
        }

        // ==================== 渲染 ====================
        function renderMarkdown(text) {
            if (!text) return '';
            return DOMPurify.sanitize(marked.parse(text));
        }

        function formatJSON(obj) {
            if (typeof obj === 'string') return obj;
            return JSON.stringify(obj, null, 2);
        }

        function truncate(text, len) {
            if (typeof text !== 'string') text = JSON.stringify(text, null, 2);
            return text.length > len ? text.slice(0, len) + '...' : text;
        }

        // ==================== 工具显示 ====================
        function toolLabel(tc) {
            const p = tc.params || {};
            const name = tc.name;
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
                    return `写入 ${shortPath(p.path || p.filePath)}`;
                case 'replace_string_in_file':
                    return `编辑 ${shortPath(p.path || p.filePath)}`;
                case 'multi_replace_string_in_file':
                    return `批量编辑 (${(p.replacements || []).length} 处)`;
                case 'search_text':
                    return `搜索 "${(p.query || '').slice(0, 30)}"`;
                case 'find_files':
                    return `查找文件 "${(p.pattern || '').slice(0, 30)}"`;
                case 'find_symbol':
                    return `查找符号 "${(p.symbol || '').slice(0, 30)}"`;
                case 'list_directory':
                    return `列出目录 ${shortPath(p.path)}`;
                case 'run_command': {
                    const cmd = p.command || '';
                    return `运行 ${cmd.length > 40 ? cmd.slice(0, 40) + '…' : cmd}`;
                }
                case 'create_task': {
                    const cmd = p.command || '';
                    return `后台任务 ${cmd.length > 35 ? cmd.slice(0, 35) + '…' : cmd}`;
                }
                case 'delete_file':
                    return `删除 ${shortPath(p.path)}`;
                case 'delete_directory':
                    return `删除目录 ${shortPath(p.path)}`;
                case 'move_file':
                case 'move_directory':
                    return `移动 ${shortPath(p.source)} → ${shortPath(p.dest)}`;
                case 'copy_file':
                    return `复制 ${shortPath(p.source)}`;
                case 'create_directory':
                    return `创建目录 ${shortPath(p.path)}`;
                case 'stat_path':
                    return `查看状态 ${shortPath(p.path)}`;
                case 'stop_task':
                    return `停止任务 ${p.task_id || ''}`;
                case 'task_status':
                    return `任务状态 ${p.task_id || ''}`;
                case 'read_stdout':
                case 'read_stderr':
                    return `读取输出 ${p.task_id || ''}`;
                case 'write_stdin':
                    return `写入输入 ${p.task_id || ''}`;
                case 'wait_task':
                    return `等待任务 ${p.task_id || ''}`;
                case 'list_tasks':
                    return '列出所有任务';
                case 'del_task':
                    return `清理任务 ${p.task_id || ''}`;
                case 'get_system_info':
                    return '获取系统信息';
                case 'fetch_webpage':
                    return `抓取 ${(p.url || '').replace(/^https?:\/\//, '').slice(0, 40)}`;
                default:
                    return name;
            }
        }

        function toolIconClass(name) {
            const map = {
                read_file: 'icon-file',
                write_file: 'icon-new-file',
                replace_string_in_file: 'icon-edit',
                multi_replace_string_in_file: 'icon-edit',
                search_text: 'icon-search',
                find_files: 'icon-file-search',
                find_symbol: 'icon-search',
                list_directory: 'icon-folder',
                run_command: 'icon-terminal',
                create_task: 'icon-terminal',
                delete_file: 'icon-trash',
                delete_directory: 'icon-trash',
                move_file: 'icon-edit',
                move_directory: 'icon-edit',
                copy_file: 'icon-new-file',
                create_directory: 'icon-folder',
                stat_path: 'icon-file',
                get_system_info: 'icon-gear',
                fetch_webpage: 'icon-globe',
                stop_task: 'icon-terminal',
                task_status: 'icon-terminal',
                read_stdout: 'icon-terminal',
                read_stderr: 'icon-terminal',
                write_stdin: 'icon-terminal',
                wait_task: 'icon-terminal',
                list_tasks: 'icon-terminal',
                del_task: 'icon-trash',
            };
            return map[name] || 'icon-gear';
        }

        function isCodeResult(tc) {
            const codeTools = ['read_file', 'run_command', 'create_task', 'read_stdout', 'read_stderr'];
            return codeTools.includes(tc.name) && typeof tc.result === 'string' && tc.result.length > 0;
        }

        // ==================== 头像生成 ====================
        function _avatarHash(str) {
            let h = 0;
            for (let i = 0; i < str.length; i++) {
                h = ((h << 5) - h) + str.charCodeAt(i);
                h = h & h;
            }
            return Math.abs(h);
        }

        function genPixelAvatar(name) {
            const colors = ['#2197a3', '#f71e6c', '#f07868', '#ebb970', '#e7d3b0'];
            const h = _avatarHash(name || 'user');
            const size = 80, grid = 8, cell = size / grid;
            let rects = '', seed = h;
            function next() { seed = (seed * 16807 + 12345) & 0x7fffffff; return seed; }
            for (let y = 0; y < grid; y++)
                for (let x = 0; x < grid; x++)
                    rects += `<rect x="${x * cell}" y="${y * cell}" width="${cell}" height="${cell}" fill="${colors[next() % 5]}"/>`;
            const svg = `<svg viewBox="0 0 ${size} ${size}" fill="none" xmlns="http://www.w3.org/2000/svg">` +
                `<mask id="px" maskUnits="userSpaceOnUse" x="0" y="0" width="${size}" height="${size}">` +
                `<rect width="${size}" height="${size}" rx="160" fill="#fff"/></mask>` +
                `<g mask="url(#px)">${rects}</g></svg>`;
            return 'data:image/svg+xml,' + encodeURIComponent(svg);
        }

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

        const userAvatar = computed(() => genPixelAvatar(activeSessionId.value || 'default'));
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

        // ==================== 滚动 & 输入框 ====================
        function scrollToBottom() {
            nextTick(() => {
                if (chatArea.value) {
                    chatArea.value.scrollTop = chatArea.value.scrollHeight;
                }
            });
        }

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

        // 切到文件视图时自动加载根目录
        watch(sidebarView, (v) => {
            if (v === 'files' && (fileTree.value.length === 0 || _fileTreeDirty)) {
                _fileTreeDirty = false;
                loadFileRoot();
            }
            // 离开设置侧边栏时关闭设置页
            if (v !== 'settings') settingsOpen.value = false;
        });

        // ==================== 侧边栏拖拽 ====================
        function startResize(e) {
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
                sash.classList.remove('active');
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

        // ==================== 编辑区分栏拖拽 ====================
        function startEditorResize(e) {
            const sash = e.target;
            sash.classList.add('active');
            const startX = e.clientX;
            const panel = sash.previousElementSibling;
            const startWidth = panel.getBoundingClientRect().width;

            function onMove(e2) {
                const delta = e2.clientX - startX;
                const newWidth = Math.max(200, startWidth + delta);
                panel.style.width = newWidth + 'px';
            }

            function onUp() {
                sash.classList.remove('active');
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

        // ==================== 全局快捷键 ====================
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

        // ==================== 认证 ====================
        async function verifyToken() {
            if (!authToken.value) return false;
            try {
                const res = await fetch(`${BASE}/api/verify`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: authToken.value }),
                });
                return res.ok;
            } catch {
                return false;
            }
        }

        async function login() {
            loginError.value = '';
            loginLoading.value = true;
            try {
                const endpoint = needsSetup.value ? `${BASE}/api/setup` : `${BASE}/api/login`;
                const res = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: loginPassword.value }),
                });
                const data = await res.json();
                if (!res.ok) {
                    loginError.value = data.error || '登录失败';
                    return;
                }
                authToken.value = data.token;
                localStorage.setItem('orion_token', data.token);
                loginPassword.value = '';
                loggedIn.value = true;
                needsSetup.value = false;
                connectWS();
            } catch (e) {
                loginError.value = '网络错误';
            } finally {
                loginLoading.value = false;
            }
        }

        function logout() {
            authToken.value = '';
            localStorage.removeItem('orion_token');
            loggedIn.value = false;
            if (ws) ws.close();
        }

        // ==================== 生命周期 ====================
        onMounted(async () => {
            // 获取配置检查是否需要设置密码
            try {
                const res = await fetch(`${BASE}/api/verify`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token: authToken.value || '' }),
                });
                if (res.ok) {
                    loggedIn.value = true;
                    connectWS();
                } else {
                    // 检查是否需要初始设置
                    const cfgRes = await fetch(`${BASE}/__auth_status`);
                    if (cfgRes.ok) {
                        const status = await cfgRes.json();
                        needsSetup.value = status.needs_setup;
                    }
                }
            } catch {
                // 网络错误，显示登录页
            }
            document.addEventListener('keydown', handleGlobalKeydown);
        });

        onUnmounted(() => {
            if (ws) ws.close();
            if (reconnectTimer) clearTimeout(reconnectTimer);
            document.removeEventListener('keydown', handleGlobalKeydown);
            window.removeEventListener('resize', _onResize);
        });

        // ==================== 导出 ====================
        return {
            // 认证
            loggedIn, needsSetup, loginError, loginLoading, loginPassword,
            login, logout,
            // 核心状态
            sessions, activeSessionId, messages, inputText,
            isConnected, isProcessing, sidebarVisible, sidebarView, settingsOpen, isMobile,
            currentModel, askOptions, effectiveCwd,
            activeSessionTitle, hasStreamingMessage, canSend, unreadCount,
            userAvatar, aiAvatar,
            chatArea, inputBox,

            // 会话操作
            createSession, switchSession, deleteSession, sendMessage,
            cancelProcessing, selectOption, handleKeydown, startResize, startEditorResize,

            // 渲染
            renderMarkdown, formatJSON, truncate, formatTime,
            toolLabel, toolIconClass, isCodeResult,
            getTextContent,

            // 设置
            settingsTab, showApiKey,
            configForm, configSaving, configSaveMsg, configSaveSuccess,
            testingLLM, testingAxon, restartingAxon,
            llmTestResult, axonTestResult,
            switchToSettings, closeSettings, toggleSidebarView, saveConfig, testLLM, testAxon, restartAxon,

            // 文件浏览
            fileTree, flatFileList, fileRootPath, fileLoading, fileError,
            openFilePath, openFileContent, openFileName, openFileHtml, openFileLineCount,
            loadFileRoot, toggleFolder, openFileEntry, closeFilePreview, mobileBackToFiles,
            getFileExtension, getFileLanguage, formatFileSize,
        };
    }
}).mount('#app');
