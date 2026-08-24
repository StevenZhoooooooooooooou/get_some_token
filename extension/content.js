(function () {
    'use strict';

    var PROXY = 'http://127.0.0.1:8787';
    var POLL_INTERVAL_MS = 1000;
    var latestToken = '';

    // 把 inject.js 注入页面主世界，由它捕获 token（Authorization 头）。
    function inject() {
        var s = document.createElement('script');
        s.src = chrome.runtime.getURL('inject.js');
        s.onload = function () { s.remove(); };
        (document.head || document.documentElement).appendChild(s);
    }
    inject();

    // 接收主世界捕获到的 token，转发给后台（后台用 chrome.cookies 补 httpOnly cookie 后推给代理）。
    window.addEventListener('message', function (ev) {
        if (ev.source !== window) return;
        var d = ev.data;
        if (d && d.type === 'HOPGPT_CAPTURE') {
            if (d.token) latestToken = d.token;
            try {
                chrome.runtime.sendMessage({
                    type: 'capture',
                    token: d.token || '',
                    cookie: d.cookie || '',
                    userAgent: d.userAgent || ''
                });
            } catch (e) {}
        }
    });

    // ---- 任务执行：代理把上游请求交给这里，用浏览器真实网络栈去调 HopGPT ----

    function postResult(jobId, payload) {
        return fetch(PROXY + '/jobs/' + jobId + '/result', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).catch(function (e) {
            console.warn('[HopGPT] 回传结果失败:', e);
        });
    }

    async function executeJob(job) {
        var token = job.token || latestToken;
        var headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + token
        };
        var postBody = {
            endpoint: job.endpoint,
            model: job.model,
            text: job.text,
            conversationId: null,
            isTemporary: false,
            isContinued: false,
            isRegenerate: false,
            messageId: crypto.randomUUID(),
            parentMessageId: '00000000-0000-0000-0000-000000000000'
        };
        if (job.endpointType) postBody.endpointType = job.endpointType;

        try {
            var r1 = await fetch('https://chat.ai.jh.edu/api/agents/chat/' + job.endpoint, {
                method: 'POST',
                credentials: 'include',
                headers: headers,
                body: JSON.stringify(postBody)
            });
            if (r1.status >= 400) {
                var t1 = await r1.text();
                console.warn('[HopGPT] 上游 POST', r1.status, t1.slice(0, 200));
                await postResult(job.job_id, { ok: false, status: r1.status, error: 'POST ' + r1.status + ': ' + t1.slice(0, 200) });
                return;
            }
            var j1 = await r1.json();
            var streamId = j1 && j1.streamId;
            if (!streamId) {
                await postResult(job.job_id, { ok: false, error: 'no streamId: ' + JSON.stringify(j1).slice(0, 200) });
                return;
            }

            var r2 = await fetch('https://chat.ai.jh.edu/api/agents/chat/stream/' + streamId, {
                credentials: 'include',
                headers: { 'Authorization': 'Bearer ' + token }
            });
            var body = await r2.text();
            console.log('[HopGPT] 上游返回', r2.status, '长度', body.length);
            await postResult(job.job_id, { ok: r2.ok, status: r2.status, body: body });
        } catch (e) {
            console.warn('[HopGPT] 执行任务异常:', e);
            await postResult(job.job_id, { ok: false, error: String(e) });
        }
    }

    function poll() {
        fetch(PROXY + '/jobs/poll')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.job) {
                    console.log('[HopGPT] 领取任务', data.job.job_id, data.job.endpoint, data.job.model);
                    executeJob(data.job);
                }
            })
            .catch(function () { /* 代理未启动时静默 */ });
    }

    poll();
    setInterval(poll, POLL_INTERVAL_MS);
    console.log('[HopGPT] content script loaded (job runner active)');
})();
