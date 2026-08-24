// Service worker：接收主世界捕获的 token，补上 httpOnly cookie，推给本地代理。
function looksJwt(v) {
    return typeof v === 'string' && v.indexOf('eyJ') === 0 && v.split('.').length === 3;
}

function getAllCookies(cb) {
    chrome.cookies.getAll({ url: 'https://chat.ai.jh.edu/' }, function (cookies) {
        var parts = [];
        for (var i = 0; i < cookies.length; i++) {
            parts.push(cookies[i].name + '=' + cookies[i].value);
        }
        cb(parts.join('; '));
    });
}

function handleCapture(msg) {
    var token = msg.token || '';
    getAllCookies(function (cookieStr) {
        if (looksJwt(token)) {
            console.log('[HopGPT] 捕获 token（' + token.length + ' 字符），cookie（' + cookieStr.length + ' 字符）');
        }
        fetch('http://127.0.0.1:8787/__capture', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: token, cookie: cookieStr, userAgent: msg.userAgent || '' })
        }).then(function (r) { return r.json(); })
          .then(function (j) {
              console.log('[HopGPT] 凭证推送:', 'token', (j.token_len || 0), '字符, cookie', (j.cookie_len || 0), '字符, 有效:', j.token_valid, '/', j.cookie_valid);
          })
          .catch(function (e) { console.warn('[HopGPT] 代理不可达:', e); });
    });
}

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
    if (msg && msg.type === 'capture') {
        handleCapture(msg);
    }
});
