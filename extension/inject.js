// 运行在页面主世界：能访问页面真实的 fetch / XHR / storage。
(function () {
    'use strict';

    var lastToken = '';

    function looksJwt(v) {
        return typeof v === 'string' && v.indexOf('eyJ') === 0 && v.split('.').length === 3;
    }

    function post(token) {
        try {
            window.postMessage({
                type: 'HOPGPT_CAPTURE',
                token: token || '',
                cookie: document.cookie || '',
                userAgent: navigator.userAgent || ''
            }, '*');
        } catch (e) {}
    }

    function grabHeader(val) {
        var m = /^Bearer\s+(.+)$/i.exec(val || '');
        if (m && looksJwt(m[1])) {
            lastToken = m[1];
            console.log('[HopGPT] 捕获 token（' + m[1].length + ' 字符）');
            post(m[1]);
        }
    }

    // 拦截页面真实的 fetch
    var origFetch = window.fetch;
    window.fetch = function () {
        try {
            var init = arguments[1] || {};
            var h = init.headers;
            var val = '';
            if (h instanceof Headers) val = h.get('authorization') || '';
            else if (h && typeof h === 'object') val = h['Authorization'] || h['authorization'] || '';
            grabHeader(val);
        } catch (e) {}
        return origFetch.apply(this, arguments);
    };

    // 拦截 XHR
    var origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
        if (name && name.toLowerCase() === 'authorization') grabHeader(value);
        return origSetHeader.apply(this, arguments);
    };

    function fromStorage() {
        for (var s = 0; s < 2; s++) {
            var store = [sessionStorage, localStorage][s];
            try {
                for (var i = 0; i < store.length; i++) {
                    var v = store.getItem(store.key(i));
                    if (!v) continue;
                    var c = v.replace(/^"|"$/g, '');
                    if (looksJwt(c)) return c;
                    try {
                        var o = JSON.parse(v);
                        for (var k in o) if (looksJwt(o[k])) return o[k];
                    } catch (e) {}
                }
            } catch (e) {}
        }
        return '';
    }

    function tick() {
        var t = looksJwt(lastToken) ? lastToken : fromStorage();
        if (!looksJwt(t)) {
            console.log('[HopGPT] 主世界脚本就绪，等待页面发起带 Authorization 的请求...');
        }
        post(t);
    }

    tick();
    setInterval(tick, 60000);
})();
