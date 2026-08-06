export const config = { matcher: '/:path*' };

const COOKIE_NAME = 'dash_session';

async function sha256Hex(str) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function loginPage(showError) {
  return `<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>123Milhas Dashboard — Login</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f172a; }
  .card { background:#fff; border-radius:12px; padding:32px; width:100%; max-width:340px;
    box-shadow: 0 10px 40px rgba(0,0,0,.3); }
  h1 { font-size:18px; margin:0 0 20px; color:#0f172a; }
  label { display:block; font-size:13px; color:#475569; margin-bottom:4px; }
  input { width:100%; padding:10px 12px; margin-bottom:14px; border:1px solid #cbd5e1;
    border-radius:8px; font-size:14px; }
  button { width:100%; padding:10px; background:#0f172a; color:#fff; border:none;
    border-radius:8px; font-size:14px; cursor:pointer; }
  button:hover { background:#1e293b; }
  .error { color:#dc2626; font-size:13px; margin-bottom:12px; }
</style>
</head>
<body>
  <form class="card" method="POST" action="/login">
    <h1>123Milhas Dashboard</h1>
    ${showError ? '<div class="error">Usuário ou senha inválidos.</div>' : ''}
    <label for="u">Usuário</label>
    <input id="u" name="username" autocomplete="username" required>
    <label for="p">Senha</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Entrar</button>
  </form>
</body>
</html>`;
}

function html(body, status) {
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

export default async function middleware(request) {
  const url = new URL(request.url);
  const user = process.env.DASH_USER;
  const pass = process.env.DASH_PASS;
  const expected = await sha256Hex(`${user}:${pass}`);

  if (url.pathname === '/login' && request.method === 'POST') {
    const form = await request.formData();
    const u = form.get('username');
    const p = form.get('password');
    if (u === user && p === pass) {
      const res = new Response(null, { status: 302, headers: { Location: '/' } });
      res.headers.append(
        'Set-Cookie',
        `${COOKIE_NAME}=${expected}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=2592000`
      );
      return res;
    }
    return html(loginPage(true), 401);
  }

  if (url.pathname === '/login') {
    return html(loginPage(false), 200);
  }

  const cookieHeader = request.headers.get('cookie') || '';
  const match = cookieHeader.match(new RegExp(`${COOKIE_NAME}=([a-f0-9]+)`));
  if (match && match[1] === expected) {
    return;
  }

  return new Response(null, { status: 302, headers: { Location: '/login' } });
}
