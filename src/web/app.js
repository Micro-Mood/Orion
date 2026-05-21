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
        const configLoaded = ref(false);
        const askOptionsMap = ref({});
        const askKindMap = ref({});   // 'confirm' | ''
        const askOptions = computed(() => askOptionsMap.value[activeSessionId.value] || []);
        const askKind = computed(() => askKindMap.value[activeSessionId.value] || '');
        const pendingConfirmMap = ref({});  // session_id → {msg_id, tools:[{id,name,args}]}
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
        const showHiddenFiles = ref(localStorage.getItem('orion_showHidden') !== 'false');

        // 计算属性: 将树展平为可渲染列表 (只输出可见节点)
        const flatFileList = computed(() => {
            const result = [];
            function walk(nodes) {
                for (const n of nodes) {
                    if (!showHiddenFiles.value && n.name.startsWith('.')) continue;
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
        const fileModified = ref(false);
        const editorContainer = ref(null);
        let _editorReady = false;
        let _saveTimestamp = 0;
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
                max_iterations: 30,
                tool_ttl_seconds: 300,
                auto_confirm_dangerous: false,
                context_window: 128000,
                compress_at: 0.55,
                context_recent_n: 4,
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

        const activeSessionTotalTokens = computed(() => {
            const s = sessions.value.find(s => s.id === activeSessionId.value);
            return s ? (s.tokens || 0) : 0;
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
        let _loadInitialAfterConfig = false;

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
                        configLoaded.value = false;
                        _loadInitialAfterConfig = true;
                        wsSend({ type: 'get_config' });
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

                session_forked: () => {
                    sessions.value.unshift(data.session);
                    // 自动切换到分又的新会话
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
                                if (seg.type === 'tool' || seg.type === 'thinking' || seg.type === 'compress') {
                                    return { ...seg, expanded: false };
                                }
                                return { ...seg };
                            }),
                        }));
                        isProcessing.value = !!data.is_running;
                        if (data.pending_options && data.pending_options.length) {
                            askOptionsMap.value[data.session_id] = data.pending_options;
                        } else {
                            delete askOptionsMap.value[data.session_id];
                        }
                        if (data.pending_confirm) {
                            pendingConfirmMap.value[data.session_id] = {
                                msg_id: data.pending_confirm.message_id,
                                tools: data.pending_confirm.tools || [],
                            };
                        } else {
                            delete pendingConfirmMap.value[data.session_id];
                        }
                        // 后台任务仍在运行：服务端会返回真实 partial message；兜底标记最后一条 AI 为 streaming
                        if (data.is_running && !messages.value.some(m => m.role === 'assistant' && m.streaming)) {
                            const last = getLastAIMessage();
                            if (last) last.streaming = true;
                        }
                        scrollToBottom();
                    }
                },

                message_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const already = findMessage(data.message_id);
                    if (already) {
                        already.streaming = true;
                        isProcessing.value = true;
                        scrollToBottom();
                        return;
                    }
                    // resume=true: 复用既有气泡（危险工具确认后继续）
                    if (data.resume) {
                        const existing = findMessage(data.message_id);
                        if (existing) {
                            existing.streaming = true;
                            isProcessing.value = true;
                            scrollToBottom();
                            return;
                        }
                    }
                    messages.value.push({
                        id: data.message_id,
                        role: 'assistant',
                        segments: [],
                        streaming: true
                    });
                    isProcessing.value = true;
                    scrollToBottom();
                },

                thinking_delta: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage();
                    if (msg) {
                        const segs = msg.segments;
                        if (segs.length > 0 && segs[segs.length - 1].type === 'thinking') {
                            segs[segs.length - 1].content += data.content;
                        } else {
                            segs.push({ type: 'thinking', content: data.content, expanded: true });
                        }
                        // 自动滚动 thinking 容器到底部 + 更新 fade
                        Vue.nextTick(() => {
                            const els = document.querySelectorAll('.thinking-content');
                            if (els.length) {
                                const el = els[els.length - 1];
                                el.scrollTop = el.scrollHeight;
                                updateThinkingFade(el);
                            }
                        });
                        scrollToBottom();
                    }
                },

                message_delta: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage();
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
                    // 同步会话级 token 计数 (无论是否是当前会话)
                    const sess = sessions.value.find(s => s.id === data.session_id);
                    if (sess) {
                        if (typeof data.prompt_tokens === 'number' && data.prompt_tokens > 0) {
                            sess.last_prompt_tokens = data.prompt_tokens;
                        }
                        if (typeof data.completion_tokens === 'number') {
                            sess.last_completion_tokens = data.completion_tokens;
                        }
                        if (typeof data.session_total_tokens === 'number') {
                            sess.tokens = data.session_total_tokens;
                        }
                    }
                    if (data.session_id !== activeSessionId.value) return;
                    let msg = findMessage(data.message_id);
                    if (!msg) {
                        // 后台任务完成：移除占位符，插入真实消息
                        const bgIdx = messages.value.findIndex(m => m.bg_running);
                        if (bgIdx !== -1) messages.value.splice(bgIdx, 1);
                        messages.value.push({
                            id: data.message_id,
                            role: 'assistant',
                            segments: data.content ? [{ type: 'text', content: data.content }] : [],
                            streaming: false,
                            tokens: data.tokens || 0,
                            prompt_tokens: data.prompt_tokens || 0,
                            completion_tokens: data.completion_tokens || 0,
                            prompt_tokens_total: data.prompt_tokens_total || 0,
                            cached_prompt_tokens: data.cached_prompt_tokens || 0,
                            cache_hit_tokens: data.cache_hit_tokens || data.cached_prompt_tokens || 0,
                            cache_miss_tokens: data.cache_miss_tokens || 0,
                        });
                        scrollToBottom();
                        return;
                    }
                    msg.streaming = false;
                    msg.tokens = data.tokens || msg.tokens || 0;
                    msg.prompt_tokens = data.prompt_tokens || msg.prompt_tokens || 0;
                    msg.completion_tokens = data.completion_tokens || msg.completion_tokens || 0;
                    msg.prompt_tokens_total = data.prompt_tokens_total || msg.prompt_tokens_total || 0;
                    msg.cached_prompt_tokens = data.cached_prompt_tokens || msg.cached_prompt_tokens || 0;
                    msg.cache_hit_tokens = data.cache_hit_tokens || data.cached_prompt_tokens || msg.cache_hit_tokens || 0;
                    msg.cache_miss_tokens = data.cache_miss_tokens || msg.cache_miss_tokens || 0;
                    // 自动折叠 thinking 段
                    msg.segments.forEach(s => {
                        if (s.type === 'thinking') s.expanded = false;
                    });
                    // 如果服务端发了最终文本且当前无文本 segment，补上
                    if (data.content) {
                        const hasText = msg.segments.some(s => s.type === 'text');
                        if (!hasText) {
                            msg.segments.push({ type: 'text', content: data.content });
                        }
                    }
                },

                tool_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        // 若已存在同 id 的 segment（例如 pending 确认后已升级为 running），
                        // 则不重复 push
                        if (data.tool_id) {
                            const existing = msg.segments.find(
                                s => s.type === 'tool' && s.id === data.tool_id
                            );
                            if (existing) {
                                existing.status = 'running';
                                scrollToBottom();
                                return;
                            }
                        }
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
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage() || getLastAIMessage();
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

                compress_start: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        msg.segments.push({
                            type: 'compress',
                            id: data.seg_id || '',
                            status: 'running',
                            archived: data.archived || 0,
                            archived_tokens: data.archived_tokens || data.prompt_tokens || 0,
                            prompt_tokens: data.archived_tokens || data.prompt_tokens || 0,
                            title: '',
                            file: '',
                            error: '',
                            expanded: false,
                        });
                        scrollToBottom();
                    }
                },

                compress_end: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        const seg = msg.segments.findLast(
                            s => s.type === 'compress' && s.status === 'running'
                        );
                        if (seg) {
                            seg.status = data.success ? 'success' : 'error';
                            seg.title = data.title || '';
                            seg.file = data.file || '';
                            seg.archived_tokens = data.archived_tokens || seg.archived_tokens || 0;
                            seg.prompt_tokens = data.archived_tokens || seg.prompt_tokens || 0;
                            seg.error = data.error || '';
                        }
                    }
                },

                pending_confirm: () => {
                    if (data.session_id !== activeSessionId.value) return;
                    const tools = data.tools || [];
                    // 初始决策：非危险工具默认 'run'，危险工具等待用户选择
                    const decisions = {};
                    for (const t of tools) {
                        if (!t.dangerous) decisions[t.id] = 'run';
                    }
                    pendingConfirmMap.value[data.session_id] = {
                        msg_id: data.message_id,
                        tools,
                        decisions,
                    };
                    // 将每个待确认工具作为 pending segment 追加到消息，
                    // 渲染 Run/Skip 按钮
                    const msg = (data.message_id && findMessage(data.message_id)) || findStreamingMessage() || getLastAIMessage();
                    if (msg) {
                        for (const t of tools) {
                            msg.segments.push({
                                type: 'tool',
                                id: t.id,
                                name: t.name,
                                params: t.args || {},
                                status: 'pending',
                                dangerous: !!t.dangerous,
                                result: null,
                                duration: null,
                                expanded: false,
                            });
                        }
                        msg.streaming = false;
                        scrollToBottom();
                    }
                    isProcessing.value = false;
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
                    askKindMap.value[data.session_id] = data.kind || '';
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
                        configForm.engine.max_iterations = cfg.engine.max_iterations ?? 30;
                        configForm.engine.tool_ttl_seconds = cfg.engine.tool_ttl_seconds ?? 300;
                        configForm.engine.auto_confirm_dangerous = cfg.engine.auto_confirm_dangerous ?? false;
                        configForm.engine.context_window = cfg.engine.context_window ?? 128000;
                        configForm.engine.compress_at = cfg.engine.compress_at ?? 0.55;
                        configForm.engine.context_recent_n = cfg.engine.context_recent_n ?? 4;
                    }
                    configLoaded.value = true;
                    if (_loadInitialAfterConfig) {
                        _loadInitialAfterConfig = false;
                        wsSend({ type: 'get_sessions' });
                        if (activeSessionId.value) {
                            wsSend({ type: 'get_messages', session_id: activeSessionId.value });
                        }
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
                        // 填充某个目录的子节点（保留已展开状态）
                        const oldMap = {};
                        for (const c of node.children) oldMap[c.name] = c;
                        node.children = raw.map(e => {
                            const old = oldMap[e.name];
                            if (old && old.type === e.type) {
                                old.size = e.size;
                                return old;
                            }
                            return {
                                name: e.name, type: e.type, size: e.size,
                                path: parentPath + sep + e.name,
                                depth: node.depth + 1,
                                expanded: false, loaded: false, loading: false, children: [],
                            };
                        });
                        node.loaded = true;
                        node.loading = false;
                        delete _pendingNodes[parentPath];
                    } else {
                        // 根级加载（保留已展开目录的状态）
                        const oldMap = {};
                        for (const c of fileTree.value) oldMap[c.name] = c;
                        fileRootPath.value = parentPath;
                        fileTree.value = raw.map(e => {
                            const old = oldMap[e.name];
                            if (old && old.type === e.type) {
                                old.size = e.size;
                                old.path = parentPath + sep + e.name;
                                return old;
                            }
                            return {
                                name: e.name, type: e.type, size: e.size,
                                path: parentPath + sep + e.name,
                                depth: 0,
                                expanded: false, loaded: false, loading: false, children: [],
                            };
                        });
                    }
                    // 刷新时重载已展开的子目录
                    const refreshed = node ? node.children : fileTree.value;
                    for (const c of refreshed) {
                        if (c.type === 'directory' && c.expanded && c.loaded) {
                            c.loading = true;
                            _pendingNodes[c.path] = c;
                            wsSend({ type: 'list_files', path: c.path });
                        }
                    }
                },

                file_content: () => {
                    fileLoading.value = false;
                    if (data.error) {
                        // 文件不存在 → 关闭预览（删除/移动场景）
                        if (openFilePath.value === data.path) {
                            destroyEditor();
                            openFilePath.value = '';
                            openFileContent.value = '';
                        }
                    } else {
                        const isNewFile = openFilePath.value !== data.path;
                        openFilePath.value = data.path || '';
                        openFileContent.value = data.content || '';

                        if (isNewFile) {
                            nextTick(() => initEditor(data.content || '', openFileName.value));
                        } else if (_editorReady) {
                            window.OrionEditor?.setValue(data.content || '');
                            fileModified.value = false;
                        }
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
                    // 跳过条件: 刚保存过(<3s) 或有未保存更改
                    if (openFilePath.value
                        && Date.now() - _saveTimestamp > 3000
                        && !fileModified.value) {
                        const changed = data.paths || [];
                        const norm = p => p.replace(/\\/g, '/');
                        if (changed.some(p => norm(p) === norm(openFilePath.value))) {
                            wsSend({ type: 'read_file_content', path: openFilePath.value });
                        }
                    }
                },

                file_saved: () => {
                    if (!data.success) {
                        alert('保存失败: ' + (data.error || '未知错误'));
                        fileModified.value = true;
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
            delete askKindMap.value[activeSessionId.value];
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

        function forkSession(msgId) {
            if (!activeSessionId.value) return;
            wsSend({
                type: 'fork_session',
                session_id: activeSessionId.value,
                message_id: msgId,
                title: '分叉对话',
            });
        }

        function setToolDecision(sessionId, toolId, decision) {
            // 记录本地决策；当所有危险工具都有决定后一次性提交 confirm_tools
            const pending = pendingConfirmMap.value[sessionId];
            if (!pending) return;
            pending.decisions = pending.decisions || {};
            pending.decisions[toolId] = decision;
            const msg = findMessage(pending.msg_id);
            if (msg) {
                for (const seg of msg.segments) {
                    if (seg.type !== 'tool' || seg.id !== toolId) continue;
                    if (decision === 'run') {
                        seg.status = 'await_run';   // 本地预设，提交后转 running
                    } else if (decision === 'skip') {
                        seg.status = 'await_skip';
                    }
                }
            }
            const allDecided = (pending.tools || [])
                .filter(t => t.dangerous)
                .every(t => pending.decisions[t.id]);
            if (!allDecided) return;
            // 所有危险工具都有决定，提交
            const confirmed = [];
            const skipped = [];
            for (const t of pending.tools || []) {
                const d = pending.decisions[t.id] || (t.dangerous ? 'skip' : 'run');
                if (d === 'run') confirmed.push(t.id);
                else skipped.push(t.id);
            }
            if (msg) {
                for (const seg of msg.segments) {
                    if (seg.type !== 'tool') continue;
                    if (seg.status === 'await_run') seg.status = 'running';
                    else if (seg.status === 'await_skip') {
                        seg.status = 'error';
                        seg.result = '用户已取消此操作';
                    }
                }
            }
            wsSend({
                type: 'confirm_tools',
                session_id: sessionId,
                confirmed,
                skipped,
            });
            delete pendingConfirmMap.value[sessionId];
            isProcessing.value = true;
        }

        function decideAllPending(sessionId, decision) {
            const pending = pendingConfirmMap.value[sessionId];
            if (!pending) return;
            for (const t of pending.tools || []) {
                if (!t.dangerous) continue;
                if (pending.decisions && pending.decisions[t.id]) continue;
                setToolDecision(sessionId, t.id, decision);
            }
        }

        function confirmTools(sessionId, confirmedIds, skippedIds) {
            // 兼容旧调用：逐个设决策后由 setToolDecision 自动提交
            for (const id of confirmedIds || []) {
                setToolDecision(sessionId, id, 'run');
            }
            for (const id of skippedIds || []) {
                setToolDecision(sessionId, id, 'skip');
            }
        }

        // ==================== 文件浏览 ====================
        function toggleShowHidden() {
            showHiddenFiles.value = !showHiddenFiles.value;
            localStorage.setItem('orion_showHidden', showHiddenFiles.value);
        }

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
                if (fileModified.value && !confirm('有未保存的更改，是否放弃？')) return;
                fileLoading.value = true;
                fileError.value = '';
                wsSend({ type: 'read_file_content', path: node.path });
                if (isMobile.value) sidebarVisible.value = false;
            }
        }

        function closeFilePreview() {
            if (fileModified.value && !confirm('有未保存的更改，是否放弃？')) return;
            destroyEditor();
            openFilePath.value = '';
            openFileContent.value = '';
        }

        function mobileBackToFiles() {
            openFilePath.value = '';
            openFileContent.value = '';
            sidebarView.value = 'files';
            sidebarVisible.value = true;
        }

        // ==================== 编辑器 ====================
        async function initEditor(content, fileName) {
            if (!window.OrionEditor) {
                await import('./editor.js');
            }
            const el = editorContainer.value;
            if (!el) return;
            fileModified.value = false;
            await window.OrionEditor.create(el, content, fileName, {
                onSave: saveFile,
                onChange: () => { fileModified.value = true; },
            });
            _editorReady = true;
        }

        function destroyEditor() {
            window.OrionEditor?.destroy();
            _editorReady = false;
            fileModified.value = false;
        }

        function saveFile() {
            if (!openFilePath.value) return;
            const content = window.OrionEditor?.getValue();
            if (content == null) return;
            _saveTimestamp = Date.now();
            wsSend({
                type: 'save_file_content',
                path: openFilePath.value,
                content: content,
            });
            fileModified.value = false;
        }

        function getFileExtension(name) {
            const dot = name.lastIndexOf('.');
            return dot > 0 ? name.substring(dot + 1).toLowerCase() : '';
        }

        const _ICON_CDN = 'https://cdn.jsdelivr.net/npm/material-icon-theme@5/icons/';
        const _EXT_ICON_MAP = {
            js: 'javascript', mjs: 'javascript', cjs: 'javascript',
            jsx: 'react', tsx: 'react_ts',
            ts: 'typescript', mts: 'typescript',
            py: 'python', pyw: 'python', pyi: 'python',
            rb: 'ruby', rs: 'rust', go: 'go', java: 'java',
            c: 'c', cpp: 'cpp', cc: 'cpp', cxx: 'cpp',
            h: 'h', hpp: 'hpp', hxx: 'hpp',
            cs: 'csharp', swift: 'swift', kt: 'kotlin', dart: 'dart',
            css: 'css', scss: 'sass', less: 'less',
            html: 'html', htm: 'html',
            vue: 'vue', svelte: 'svelte',
            json: 'json', jsonc: 'json', json5: 'json',
            yaml: 'yaml', yml: 'yaml', toml: 'toml',
            xml: 'xml', svg: 'svg',
            md: 'markdown', mdx: 'mdx', txt: 'document',
            sh: 'console', bash: 'console', zsh: 'console',
            bat: 'command', cmd: 'command', ps1: 'powershell',
            sql: 'database', db: 'database', sqlite: 'database',
            png: 'image', jpg: 'image', jpeg: 'image', gif: 'image',
            webp: 'image', ico: 'image', bmp: 'image',
            mp3: 'audio', wav: 'audio', flac: 'audio', ogg: 'audio',
            mp4: 'video', mov: 'video', avi: 'video', mkv: 'video',
            zip: 'zip', tar: 'zip', gz: 'zip', rar: 'zip', '7z': 'zip',
            pdf: 'pdf', doc: 'word', docx: 'word', xls: 'table', xlsx: 'table',
            ppt: 'powerpoint', pptx: 'powerpoint',
            dockerfile: 'docker', dockerignore: 'docker',
            gitignore: 'git', gitattributes: 'git', gitmodules: 'git',
            env: 'tune', ini: 'settings', cfg: 'settings', conf: 'settings',
            lock: 'lock', log: 'log', csv: 'table',
            r: 'r', lua: 'lua', php: 'php', pl: 'perl',
            ex: 'elixir', exs: 'elixir', erl: 'erlang',
            zig: 'zig', nim: 'nim', v: 'vlang',
            proto: 'proto', graphql: 'graphql', gql: 'graphql',
            wasm: 'webassembly',
        };
        const _NAME_ICON_MAP = {
            'dockerfile': 'docker',
            'makefile': 'makefile',
            'cmakelists.txt': 'cmake',
            'license': 'license', 'licence': 'license',
            'readme.md': 'readme', 'readme': 'readme',
            'package.json': 'nodejs', 'package-lock.json': 'npm',
            'tsconfig.json': 'tsconfig', 'jsconfig.json': 'jsconfig',
            '.gitignore': 'git', '.gitattributes': 'git',
            '.env': 'tune', '.env.local': 'tune', '.env.example': 'tune',
            'requirements.txt': 'python-misc', 'setup.py': 'python-misc',
            'pyproject.toml': 'python-misc', 'pipfile': 'python-misc',
            'cargo.toml': 'rust', 'cargo.lock': 'rust',
            'go.mod': 'go-mod', 'go.sum': 'go-mod',
            'yarn.lock': 'yarn', 'pnpm-lock.yaml': 'pnpm',
            '.prettierrc': 'prettier', '.eslintrc': 'eslint',
            '.eslintrc.js': 'eslint', '.eslintrc.json': 'eslint',
            'vite.config.js': 'vite', 'vite.config.ts': 'vite',
            'webpack.config.js': 'webpack',
            'docker-compose.yml': 'docker', 'docker-compose.yaml': 'docker',
            '.editorconfig': 'editorconfig',
        };

        function getFileIconUrl(name) {
            const lower = name.toLowerCase();
            let icon = _NAME_ICON_MAP[lower];
            if (!icon) {
                const ext = getFileExtension(lower);
                icon = _EXT_ICON_MAP[ext] || 'file';
            }
            return _ICON_CDN + icon + '.svg';
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
                    max_iterations: configForm.engine.max_iterations,
                    tool_ttl_seconds: configForm.engine.tool_ttl_seconds,
                    auto_confirm_dangerous: configForm.engine.auto_confirm_dangerous,
                    context_window: configForm.engine.context_window,
                    compress_at: configForm.engine.compress_at,
                    context_recent_n: configForm.engine.context_recent_n,
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

        // ==================== Thinking ====================
        const thinkingRefs = {};

        function updateThinkingFade(el) {
            if (!el) return;
            const top = el.scrollTop > 5;
            const bottom = el.scrollTop + el.clientHeight < el.scrollHeight - 5;
            el.classList.toggle('fade-top', top);
            el.classList.toggle('fade-bottom', bottom);
        }

        // ==================== 渲染 ====================
        function renderMarkdown(text) {
            if (!text) return '';
            const html = DOMPurify.sanitize(marked.parse(text));
            // Inject copy button into <pre> blocks (after sanitization, safe because we control the markup)
            return html.replace(/<pre>/g,
                '<pre><button class="code-copy-btn" title="复制">' +
                '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5">' +
                '<rect x="5" y="5" width="8" height="8" rx="1"/><path d="M3 11V3a1 1 0 011-1h8"/></svg></button>');
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
                case 'set_session_title':
                    return `设置标题 "${(p.title || '').slice(0, 20)}"`;
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
                set_session_title: 'icon-edit',
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

        // Format tool result for human-readable display (unified for all tools)
        // Highlight tool result as JSON or plain text
        function highlightResult(r) {
            if (!r) return '';
            let text;
            if (typeof r === 'string') {
                text = truncate(r, 800);
            } else {
                text = truncate(JSON.stringify(r, null, 2), 800);
            }
            // Try highlight as JSON, fallback to plain escaped text
            try {
                return hljs.highlight(text, { language: 'json' }).value;
            } catch {
                return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            }
        }

        // Per-tool specialized rendering
        function renderToolResult(seg) {
            if (!seg || seg.result === null || seg.result === undefined || seg.result === '') return '';
            const name = seg.name || '';
            const result = seg.result;
            const params = seg.params || {};

            function esc(s) {
                return String(s)
                    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            }

            function badge(text, cls) {
                return `<span class="tool-badge ${cls || ''}">${esc(text)}</span>`;
            }

            function chip(label, value, cls) {
                return `<span class="tool-badge ${cls || ''}">${esc(label)}<span style="opacity:.7;margin-left:4px">${esc(value)}</span></span>`;
            }

            function codeBlock(code, lang) {
                let highlighted;
                try {
                    highlighted = lang
                        ? hljs.highlight(String(code), { language: lang }).value
                        : hljs.highlightAuto(String(code), ['json', 'bash', 'python', 'javascript']).value;
                } catch {
                    highlighted = esc(code);
                }
                return `<pre class="tool-code-block"><code>${highlighted}</code></pre>`;
            }

            // 试解析为 JSON; 老数据可能被截断成残缺 JSON, 做尾部回溯抢救
            function tryJson(r) {
                if (r && typeof r === 'object') return r;
                const s = String(r == null ? '' : r);
                try { return JSON.parse(s); } catch {}
                // 兜底: 截掉末尾 "... (truncated)" 之类提示, 再尝试回溯到最后一个 } 或 ]
                let t = s.replace(/\n?\.\.\..*$/s, '').trimEnd();
                for (let i = 0; i < 6; i++) {
                    const lastCurly = t.lastIndexOf('}');
                    const lastBracket = t.lastIndexOf(']');
                    const cut = Math.max(lastCurly, lastBracket);
                    if (cut <= 0) break;
                    t = t.slice(0, cut + 1);
                    try { return JSON.parse(t); } catch {}
                    t = t.slice(0, cut);
                }
                return null;
            }

            // 编辑卡片专用: before / after 双栏对比 (简易, 不做行级 diff)
            function renderDiffBlock(oldStr, newStr, fp) {
                const ext = String(fp || '').split('.').pop() || '';
                const langMap = { js:'javascript', ts:'typescript', py:'python', json:'json',
                                  md:'markdown', css:'css', html:'html', sh:'bash',
                                  yaml:'yaml', toml:'toml', rs:'rust', go:'go', java:'java',
                                  c:'c', cpp:'cpp', h:'c' };
                const lang = langMap[ext] || 'plaintext';
                function hl(code) {
                    try {
                        return hljs.highlight(String(code), { language: lang }).value;
                    } catch {
                        return esc(code);
                    }
                }
                const oldSafe = truncate(String(oldStr || ''), 1200);
                const newSafe = truncate(String(newStr || ''), 1200);
                return `<div class="tool-diff">
  <div class="tool-diff-pane tool-diff-old">
    <div class="tool-diff-label">− 原内容</div>
    <pre class="tool-code-block"><code>${hl(oldSafe)}</code></pre>
  </div>
  <div class="tool-diff-pane tool-diff-new">
    <div class="tool-diff-label">+ 新内容</div>
    <pre class="tool-code-block"><code>${hl(newSafe)}</code></pre>
  </div>
</div>`;
            }

            function fmtSize(n) {
                if (typeof n !== 'number') return n;
                if (n < 1024) return n + ' B';
                if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
                return (n / 1024 / 1024).toFixed(2) + ' MB';
            }

            // 任务状态 → badge 样式映射
            function taskStateClass(state) {
                const s = String(state || '').toLowerCase();
                if (s === 'completed') return 'badge-ok';
                if (s === 'running' || s === 'created') return 'badge-info';
                if (s === 'failed' || s === 'killed' || s === 'timed_out') return 'badge-err';
                if (s === 'stopped') return 'badge-warn';
                return '';
            }

            // ========== run_command / create_task ==========
            if (name === 'run_command' || name === 'create_task') {
                const parsed = tryJson(result);
                const cmd = (parsed && parsed.command) || params.command || '';
                if (parsed && typeof parsed === 'object') {
                    const stdout = parsed.stdout || parsed.output || '';
                    const stderr = parsed.stderr || '';
                    const exitCode = parsed.exit_code ?? parsed.returncode ?? null;
                    const taskId = parsed.task_id || '';
                    const state = parsed.state || '';
                    const pid = parsed.pid;
                    const duration = parsed.duration_ms;
                    const meta = [];
                    if (state) meta.push(`<span class="tool-badge ${taskStateClass(state)}">${esc(state)}</span>`);
                    if (exitCode !== null) meta.push(badge('exit ' + exitCode, exitCode === 0 ? 'badge-ok' : 'badge-err'));
                    if (pid != null) meta.push(chip('pid', pid));
                    if (taskId) meta.push(chip('task', String(taskId).slice(0, 12)));
                    if (duration != null) meta.push(chip('time', duration + 'ms'));
                    let html = '';
                    if (cmd) html += `<div class="tool-cmd-line">${codeBlock(String(cmd), 'bash')}</div>`;
                    if (meta.length) html += `<div class="tool-meta-row" style="margin-top:6px">${meta.join('')}</div>`;
                    if (stdout) html += codeBlock(truncate(stdout, 1500), 'bash');
                    if (stderr) {
                        html += `<div class="tool-result-line" style="margin-top:6px">${badge('stderr', 'badge-err')}</div>`;
                        html += codeBlock(truncate(stderr, 800), 'bash');
                    }
                    if (!stdout && !stderr && name === 'run_command') html += `<div class="tool-dim">(no output)</div>`;
                    return html;
                }
                let html = '';
                if (cmd) html += `<div class="tool-cmd-line">${codeBlock(String(cmd), 'bash')}</div>`;
                html += codeBlock(truncate(result, 1500), 'bash');
                return html;
            }

            // ========== read_file ==========
            if (name === 'read_file') {
                const parsed = tryJson(result);
                const path = (parsed && parsed.path) || params.path || params.filePath || '';
                const ext = path.split('.').pop() || '';
                const langMap = { js:'javascript', ts:'typescript', py:'python', json:'json', md:'markdown',
                                   css:'css', html:'html', sh:'bash', yaml:'yaml', toml:'toml', rs:'rust' };
                const lang = langMap[ext] || ext || 'plaintext';
                let content = '';
                const meta = [];
                if (parsed && typeof parsed === 'object') {
                    content = parsed.content ?? '';
                    if (parsed.size != null)  meta.push(chip('size', fmtSize(parsed.size)));
                    if (parsed.lines != null) meta.push(chip('lines', parsed.lines));
                    if (parsed.encoding)      meta.push(chip('enc', parsed.encoding));
                    if (parsed.truncated)     meta.push(badge('truncated', 'badge-info'));
                } else {
                    content = String(result);
                }
                let html = '';
                if (path) html += `<div class="tool-result-file-header">${esc(path)}</div>`;
                if (meta.length) html += `<div class="tool-meta-row" style="margin-top:0;margin-bottom:6px">${meta.join('')}</div>`;
                html += codeBlock(truncate(content, 2000), lang);
                return html;
            }

            // ========== write_file / replace / multi_replace / delete / move / copy / create_dir ==========
            if (['write_file','replace_string_in_file','multi_replace_string_in_file',
                 'delete_file','delete_directory','move_file','move_directory',
                 'copy_file','create_directory'].includes(name)) {
                const verbMap = { write_file: 'Saved', replace_string_in_file: 'Edited',
                    multi_replace_string_in_file: 'Edited', delete_file: 'Deleted',
                    delete_directory: 'Deleted', move_file: 'Moved', move_directory: 'Moved',
                    copy_file: 'Copied', create_directory: 'Created' };
                const verb = verbMap[name] || 'Done';
                const parsed = tryJson(result);
                const path = (parsed && parsed.path) || params.path || params.filePath || params.source || '';
                const dest = (parsed && parsed.dest) || params.dest || '';
                const isErr = seg.status === 'error';
                const meta = [];
                if (parsed && typeof parsed === 'object') {
                    if (parsed.size != null)       meta.push(chip('size', fmtSize(parsed.size)));
                    if (parsed.encoding)           meta.push(chip('enc', parsed.encoding));
                    if (parsed.created === true)   meta.push(badge('new', 'badge-ok'));
                    if (parsed.replaced != null)   meta.push(chip('replaced', parsed.replaced));
                    if (parsed.matches != null)    meta.push(chip('matches', parsed.matches));
                }
                let html = `<div class="tool-result-simple${isErr ? ' is-err' : ''}">`;
                html += `<span class="verb">${esc(verb)}</span>`;
                if (path) html += `<span>${esc(path)}</span>`;
                if (dest) html += `<span style="opacity:.6">→</span><span>${esc(dest)}</span>`;
                html += `</div>`;
                if (meta.length) html += `<div class="tool-meta-row">${meta.join('')}</div>`;

                // 编辑类: 展示 before / after 内容
                if (name === 'replace_string_in_file' && !isErr) {
                    const oldStr = params.old_string ?? params.oldString ?? '';
                    const newStr = params.new_string ?? params.newString ?? '';
                    if (oldStr || newStr) {
                        html += renderDiffBlock(oldStr, newStr, path);
                    }
                } else if (name === 'multi_replace_string_in_file' && !isErr) {
                    const reps = Array.isArray(params.replacements) ? params.replacements : [];
                    if (reps.length) {
                        html += `<div class="tool-diff-list">`;
                        reps.slice(0, 5).forEach((r, i) => {
                            const o = r.old_string ?? r.oldString ?? '';
                            const n = r.new_string ?? r.newString ?? '';
                            const fp = r.filePath ?? r.path ?? path;
                            html += `<div class="tool-diff-item-head">#${i + 1}${fp ? ' · ' + esc(fp) : ''}</div>`;
                            html += renderDiffBlock(o, n, fp);
                        });
                        if (reps.length > 5) {
                            html += `<div class="tool-dim">…还有 ${reps.length - 5} 处未显示</div>`;
                        }
                        html += `</div>`;
                    }
                }

                if (isErr && typeof result === 'string' && result) {
                    html += `<div class="tool-dim" style="margin-top:6px">${esc(truncate(result, 300))}</div>`;
                }
                return html;
            }

            // ========== list_directory ==========
            if (name === 'list_directory') {
                const parsed = tryJson(result);
                let entries = Array.isArray(parsed) ? parsed
                            : (parsed && Array.isArray(parsed.entries)) ? parsed.entries
                            : null;
                if (entries) {
                    if (entries.length === 0) return `<div class="tool-dim">(空目录)</div>`;
                    // 排序: 目录在前
                    const sorted = [...entries].sort((a, b) => {
                        const ad = typeof a === 'object' && (a.type === 'directory' || a.is_dir) ? 0 : 1;
                        const bd = typeof b === 'object' && (b.type === 'directory' || b.is_dir) ? 0 : 1;
                        if (ad !== bd) return ad - bd;
                        const an = typeof a === 'string' ? a : (a.name || '');
                        const bn = typeof b === 'string' ? b : (b.name || '');
                        return an.localeCompare(bn);
                    });
                    const shown = sorted.slice(0, 200);
                    const rows = shown.map(e => {
                        const n = typeof e === 'string' ? e : (e.name || e.path || JSON.stringify(e));
                        const isDir = typeof e === 'object' && (e.type === 'directory' || e.is_dir);
                        const size = typeof e === 'object' && typeof e.size === 'number' && !isDir
                            ? `<span class="tool-dir-size">${fmtSize(e.size)}</span>` : '';
                        return `<span class="tool-dir-entry ${isDir ? 'is-dir' : 'is-file'}">${esc(n)}${isDir ? '/' : ''}${size}</span>`;
                    });
                    let html = '';
                    if (parsed && parsed.path) {
                        html += `<div class="tool-result-file-header">${esc(parsed.path)} <span class="tool-dim" style="font-weight:normal">(${entries.length} 项)</span></div>`;
                    }
                    html += `<div class="tool-result-list">${rows.join('')}</div>`;
                    if (entries.length > 200) html += `<div class="tool-dim" style="margin-top:4px">…共 ${entries.length} 项，已显示前 200</div>`;
                    return html;
                }
                return codeBlock(truncate(result, 1000), 'json');
            }

            // ========== find_files ==========
            if (name === 'find_files') {
                const parsed = tryJson(result);
                const matches = Array.isArray(parsed) ? parsed
                              : (parsed && Array.isArray(parsed.matches)) ? parsed.matches
                              : null;
                if (matches) {
                    if (matches.length === 0) {
                        const pat = parsed && parsed.pattern ? ` "${esc(parsed.pattern)}"` : '';
                        return `<div class="tool-dim">(无匹配${pat})</div>`;
                    }
                    const total = (parsed && parsed.total) || matches.length;
                    const shown = matches.slice(0, 100);
                    const rows = shown.map(m => {
                        const rel = m.relative || m.path || m.name || '';
                        const size = typeof m.size === 'number' ? `<span class="tool-find-size">${fmtSize(m.size)}</span>` : '';
                        return `<div class="tool-find-row"><span class="tool-find-path">${esc(rel)}</span>${size}</div>`;
                    });
                    let html = '';
                    if (parsed && parsed.pattern) {
                        const trunc = parsed.truncated ? ' (截断)' : '';
                        html += `<div class="tool-result-file-header">${esc(parsed.pattern)} <span class="tool-dim" style="font-weight:normal">· ${total} 项${trunc}</span></div>`;
                    }
                    html += `<div class="tool-result-find">${rows.join('')}</div>`;
                    if (matches.length > 100) html += `<div class="tool-dim" style="margin-top:4px">…共 ${matches.length} 项，已显示前 100</div>`;
                    return html;
                }
                return codeBlock(truncate(result, 1000), 'json');
            }

            // ========== search_text ==========
            if (name === 'search_text') {
                const parsed = tryJson(result);
                const matches = (parsed && Array.isArray(parsed.matches)) ? parsed.matches : null;
                if (matches) {
                    if (matches.length === 0) return `<div class="tool-dim">(无匹配)</div>`;
                    const totalHits = parsed.total_hits ?? '';
                    const totalFiles = parsed.total_files_matched ?? matches.length;
                    const shownFiles = matches.slice(0, 20);
                    let body = '';
                    for (const fileM of shownFiles) {
                        const rel = fileM.relative || fileM.path || '';
                        const hits = Array.isArray(fileM.hits) ? fileM.hits : [];
                        body += `<div class="tool-search-file"><span class="tool-search-filepath">${esc(rel)}</span> <span class="tool-dim">${hits.length} hit</span></div>`;
                        const shownHits = hits.slice(0, 8);
                        for (const h of shownHits) {
                            const ln = h.line ?? '';
                            const txt = String(h.content || '').slice(0, 200);
                            body += `<div class="tool-search-hit"><span class="tool-search-ln">${esc(ln)}</span><span class="tool-search-text">${esc(txt)}</span></div>`;
                        }
                        if (hits.length > 8) {
                            body += `<div class="tool-dim" style="padding-left:18px">…还有 ${hits.length - 8} 个匹配</div>`;
                        }
                    }
                    let html = '';
                    if (parsed.query) {
                        html += `<div class="tool-result-file-header">"${esc(parsed.query)}" <span class="tool-dim" style="font-weight:normal">· ${totalFiles} 文件 / ${totalHits} 匹配</span></div>`;
                    }
                    html += `<div class="tool-result-search">${body}</div>`;
                    if (matches.length > 20) html += `<div class="tool-dim" style="margin-top:4px">…共 ${matches.length} 文件，已显示前 20</div>`;
                    return html;
                }
                return codeBlock(truncate(result, 1000), 'json');
            }

            // ========== find_symbol ==========
            if (name === 'find_symbol') {
                const parsed = tryJson(result);
                const matches = Array.isArray(parsed) ? parsed
                              : (parsed && Array.isArray(parsed.matches)) ? parsed.matches
                              : null;
                if (matches) {
                    if (matches.length === 0) return `<div class="tool-dim">(未找到符号)</div>`;
                    const rows = matches.slice(0, 100).map(m => {
                        const file = m.file || m.path || m.relative || '';
                        const line = m.line || m.line_number || '';
                        const kind = m.kind || m.type || '';
                        const sym = m.symbol || m.name || '';
                        const loc = file ? `${esc(file)}${line ? ':' + line : ''}` : '';
                        const kindTag = kind ? `<span class="tool-badge badge-info">${esc(kind)}</span>` : '';
                        return `<div class="tool-match-line">${kindTag}<span class="tool-match-sym">${esc(sym)}</span>${loc ? `<span class="tool-match-loc">${loc}</span>` : ''}</div>`;
                    });
                    let html = `<div class="tool-result-search">${rows.join('')}</div>`;
                    if (matches.length > 100) html += `<div class="tool-dim" style="margin-top:4px">…共 ${matches.length} 个匹配</div>`;
                    return html;
                }
                return codeBlock(truncate(result, 1000), 'json');
            }

            // ========== fetch_webpage ==========
            if (name === 'fetch_webpage') {
                const url = params.url || '';
                const parsed = tryJson(result);
                const text = (parsed && (parsed.content || parsed.text)) || result;
                const status = parsed && parsed.status_code != null ? parsed.status_code : null;
                const ctype = parsed && parsed.content_type ? parsed.content_type : '';
                const meta = [];
                if (status !== null) meta.push(badge(`HTTP ${status}`, status >= 400 ? 'badge-err' : 'badge-ok'));
                if (ctype) meta.push(chip('type', ctype.split(';')[0]));
                if (typeof text === 'string') meta.push(chip('len', fmtSize(text.length)));
                let html = '';
                if (url) html += `<div class="tool-result-file-header"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a></div>`;
                if (meta.length) html += `<div class="tool-meta-row" style="margin-bottom:6px">${meta.join('')}</div>`;
                html += `<div class="tool-result-text">${esc(truncate(text, 1500))}</div>`;
                return html;
            }

            // ========== stat_path / get_system_info (递归 KV，支持嵌套) ==========
            if (name === 'stat_path' || name === 'get_system_info') {
                const obj = tryJson(result);
                function fmtVal(k, v) {
                    if (typeof v === 'number') {
                        if (/size|bytes|total|used|free|available/i.test(k)) return fmtSize(v);
                        if (/percent|usage/i.test(k)) return v.toFixed(1) + '%';
                    }
                    if (typeof v === 'boolean') return v ? '✓' : '✗';
                    if (v === null || v === undefined) return '—';
                    return String(v);
                }
                function renderKv(o) {
                    const rows = Object.entries(o).map(([k, v]) => {
                        if (v && typeof v === 'object' && !Array.isArray(v)) {
                            return `<tr><td class="tool-kv-key">${esc(k)}</td><td>${renderKv(v)}</td></tr>`;
                        }
                        if (Array.isArray(v)) {
                            const list = v.slice(0, 20).map(x =>
                                typeof x === 'object' ? esc(JSON.stringify(x)) : esc(String(x))
                            ).join('<br>');
                            return `<tr><td class="tool-kv-key">${esc(k)}</td><td>${list}</td></tr>`;
                        }
                        return `<tr><td class="tool-kv-key">${esc(k)}</td><td>${esc(fmtVal(k, v))}</td></tr>`;
                    }).join('');
                    return `<table class="tool-result-kv">${rows}</table>`;
                }
                if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
                    return renderKv(obj);
                }
            }

            // ========== read_stdout / read_stderr (纯输出展示) ==========
            if (name === 'read_stdout' || name === 'read_stderr') {
                const parsed = tryJson(result);
                const output = (parsed && (parsed.output ?? '')) || '';
                const eof = parsed && parsed.eof;
                const taskId = parsed && parsed.task_id;
                const meta = [];
                if (taskId) meta.push(chip('task', String(taskId).slice(0, 12)));
                meta.push(badge(name === 'read_stderr' ? 'stderr' : 'stdout',
                                name === 'read_stderr' ? 'badge-err' : 'badge-info'));
                if (eof) meta.push(badge('eof', 'badge-info'));
                let html = `<div class="tool-meta-row" style="margin-bottom:6px">${meta.join('')}</div>`;
                if (output) {
                    html += codeBlock(truncate(String(output), 2000), 'bash');
                } else {
                    html += `<div class="tool-dim">(无新输出)</div>`;
                }
                return html;
            }

            // ========== write_stdin ==========
            if (name === 'write_stdin') {
                const parsed = tryJson(result);
                const written = parsed && parsed.written;
                const eof = parsed && parsed.eof;
                const data = params.data || '';
                let html = `<div class="tool-result-simple"><span class="verb">Wrote</span><span>${esc(written ?? '?')} chars</span>`;
                if (eof) html += `<span style="opacity:.7">+ EOF</span>`;
                html += `</div>`;
                if (data) html += codeBlock(truncate(String(data), 400), 'bash');
                return html;
            }

            // ========== list_tasks ==========
            if (name === 'list_tasks') {
                const parsed = tryJson(result);
                const tasks = (parsed && Array.isArray(parsed.tasks)) ? parsed.tasks : null;
                if (tasks) {
                    if (tasks.length === 0) return `<div class="tool-dim">(无任务)</div>`;
                    const total = parsed.total ?? tasks.length;
                    const active = parsed.active ?? 0;
                    let html = `<div class="tool-meta-row" style="margin-bottom:6px">
                        ${chip('total', total)}${chip('active', active)}
                    </div>`;
                    const rows = tasks.slice(0, 50).map(t => {
                        const cmd = String(t.command || '').slice(0, 80);
                        const stateCls = taskStateClass(t.state);
                        return `<div class="tool-task-row">
                            <span class="tool-badge ${stateCls}">${esc(t.state || '')}</span>
                            <span class="tool-task-id">${esc(String(t.task_id || '').slice(0, 12))}</span>
                            <span class="tool-task-cmd">${esc(cmd)}</span>
                        </div>`;
                    }).join('');
                    html += `<div class="tool-task-list">${rows}</div>`;
                    if (tasks.length > 50) html += `<div class="tool-dim">…共 ${tasks.length} 个任务，已显示前 50</div>`;
                    return html;
                }
                return codeBlock(truncate(result, 1000), 'json');
            }

            // ========== create_task / task_status / wait_task / stop_task / del_task ==========
            if (name === 'create_task' || name === 'task_status' || name === 'wait_task'
                || name === 'stop_task' || name === 'del_task') {
                const parsed = tryJson(result);
                if (parsed && typeof parsed === 'object') {
                    const cmd = parsed.command || params.command || '';
                    const state = parsed.state || '';
                    const pid = parsed.pid;
                    const exitCode = parsed.exit_code;
                    const duration = parsed.duration_ms;
                    const taskId = parsed.task_id;
                    const signal = parsed.signal;
                    const meta = [];
                    if (state) meta.push(`<span class="tool-badge ${taskStateClass(state)}">${esc(state)}</span>`);
                    if (exitCode !== null && exitCode !== undefined)
                        meta.push(badge('exit ' + exitCode, exitCode === 0 ? 'badge-ok' : 'badge-err'));
                    if (pid != null) meta.push(chip('pid', pid));
                    if (taskId) meta.push(chip('task', String(taskId).slice(0, 12)));
                    if (duration != null) meta.push(chip('time', duration + 'ms'));
                    if (signal) meta.push(chip('signal', signal));
                    if (parsed.deleted) meta.push(badge('deleted', 'badge-info'));
                    let html = '';
                    if (cmd) {
                        html += `<div class="tool-cmd-line">${codeBlock(String(cmd), 'bash')}</div>`;
                    }
                    if (meta.length) html += `<div class="tool-meta-row" style="margin-top:6px">${meta.join('')}</div>`;
                    return html || codeBlock(truncate(result, 800), 'json');
                }
                return codeBlock(truncate(result, 800), 'json');
            }

            // ========== set_session_title 等：简短即可 ==========
            if (typeof result === 'string' && result.length < 120 && !/[{[\n]/.test(result)) {
                return `<div class="tool-result-simple"><span>${esc(result)}</span></div>`;
            }

            // default: JSON highlight
            return codeBlock(truncate(result, 800), 'json');
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

        function formatTokens(n) {
            if (!n || n === 0) return '0';
            if (n < 1000) return n.toString();
            return (n / 1000).toFixed(1) + 'k';
        }

        // 消息的 ctx 占模型窗口百分比：最后一次 LLM 输入约等于当前上下文体量。
        function ctxPercent(msg) {
            if (!configLoaded.value) return '';
            const ctx = msg.prompt_tokens || 0;
            const win = configForm.engine.context_window || 0;
            if (win <= 0 || ctx <= 0) return '0%';
            const pct = Math.min(100, (ctx / win) * 100);
            return pct < 10 ? pct.toFixed(1) + '%' : pct.toFixed(0) + '%';
        }

        function cacheHitTokens(msg) {
            return msg.cache_hit_tokens || msg.cached_prompt_tokens || 0;
        }

        function cacheMissTokens(msg) {
            if (msg.cache_miss_tokens) return msg.cache_miss_tokens;
            if (msg.prompt_tokens && msg.cached_prompt_tokens) {
                return Math.max(0, msg.prompt_tokens - msg.cached_prompt_tokens);
            }
            return 0;
        }

        function turnPromptTokens(msg) {
            return msg.prompt_tokens_total || (cacheHitTokens(msg) + cacheMissTokens(msg)) || msg.prompt_tokens || 0;
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
            e.preventDefault();
            const sash = e.target;
            sash.classList.add('active');
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'col-resize';
            const startX = e.clientX;
            const startWidth = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width'));

            function onMove(e2) {
                const delta = e2.clientX - startX;
                const maxWidth = Math.min(500, window.innerWidth - 352);
                const newWidth = Math.max(150, Math.min(maxWidth, startWidth + delta));
                document.documentElement.style.setProperty('--sidebar-width', newWidth + 'px');
            }

            function onUp() {
                sash.classList.remove('active');
                document.body.style.userSelect = '';
                document.body.style.cursor = '';
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

        // ==================== 编辑区分栏拖拽 ====================
        function startEditorResize(e) {
            e.preventDefault();
            const sash = e.target;
            sash.classList.add('active');
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'col-resize';

            // 全屏覆盖层：防止 CM6 编辑器捕获鼠标事件
            const overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;cursor:col-resize;';
            document.body.appendChild(overlay);

            const panel = sash.closest('.editor-content').querySelector('.file-preview-panel');
            if (!panel) { overlay.remove(); return; }
            const container = sash.closest('.editor-content');
            const containerWidth = container.getBoundingClientRect().width;
            const panelLeft = panel.getBoundingClientRect().left;
            // 鼠标在 sash 内的偏移(保持 sash 与鼠标相对位置不变)
            const clickOffset = e.clientX - panel.getBoundingClientRect().right;

            function onMove(e2) {
                const rawWidth = e2.clientX - panelLeft - clickOffset;
                const newWidth = Math.max(200, Math.min(containerWidth - 304, rawWidth));
                panel.style.width = newWidth + 'px';
            }

            function onUp() {
                sash.classList.remove('active');
                document.body.style.userSelect = '';
                document.body.style.cursor = '';
                overlay.remove();
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            }

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }

        // ==================== 全局快捷键 ====================
        function handleGlobalKeydown(e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                if (openFilePath.value && fileModified.value) saveFile();
            }
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

            // Code block copy button (event delegation)
            document.addEventListener('click', (e) => {
                const btn = e.target.closest('.code-copy-btn');
                if (!btn) return;
                e.stopPropagation();
                const pre = btn.closest('pre');
                if (!pre) return;
                const code = pre.querySelector('code');
                const text = (code || pre).textContent;
                navigator.clipboard.writeText(text).then(() => {
                    btn.classList.add('copied');
                    btn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 8 7 11 12 5"/></svg>';
                    setTimeout(() => {
                        btn.classList.remove('copied');
                        btn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="5" y="5" width="8" height="8" rx="1"/><path d="M3 11V3a1 1 0 011-1h8"/></svg>';
                    }, 1500);
                });
            });
        });

        onUnmounted(() => {
            destroyEditor();
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
            currentModel, askOptions, askKind, effectiveCwd,
            activeSessionTitle, activeSessionTotalTokens, configLoaded,
            hasStreamingMessage, canSend, unreadCount,
            userAvatar, aiAvatar,
            chatArea, inputBox,

            // 会话操作
            createSession, switchSession, deleteSession, forkSession, sendMessage,
            cancelProcessing, selectOption, handleKeydown, startResize, startEditorResize,
            confirmTools, setToolDecision, decideAllPending, pendingConfirmMap,

            // 渲染
            renderMarkdown, formatJSON, truncate, formatTime, formatTokens, ctxPercent,
            cacheHitTokens, cacheMissTokens, turnPromptTokens,
            toolLabel, toolIconClass, highlightResult, renderToolResult,
            getTextContent,
            thinkingRefs, updateThinkingFade,

            // 设置
            settingsTab, showApiKey,
            configForm, configSaving, configSaveMsg, configSaveSuccess,
            testingLLM, testingAxon, restartingAxon,
            llmTestResult, axonTestResult,
            switchToSettings, closeSettings, toggleSidebarView, saveConfig, testLLM, testAxon, restartAxon,

            // 文件浏览
            fileTree, flatFileList, fileRootPath, fileLoading, fileError,
            openFilePath, openFileContent, openFileName, openFileHtml, openFileLineCount,
            fileModified, editorContainer,
            showHiddenFiles, toggleShowHidden,
            loadFileRoot, toggleFolder, openFileEntry, closeFilePreview, mobileBackToFiles,
            saveFile,
            getFileExtension, getFileLanguage, getFileIconUrl, formatFileSize,
        };
    }
}).mount('#app');
