export const config = { matcher: '/:path*' };

export default function middleware(request) {
  const user = process.env.DASH_USER;
  const pass = process.env.DASH_PASS;

  const auth = request.headers.get('authorization');
  if (auth && auth.startsWith('Basic ')) {
    const decoded = atob(auth.slice(6));
    const sep = decoded.indexOf(':');
    const u = decoded.slice(0, sep);
    const p = decoded.slice(sep + 1);
    if (u === user && p === pass) {
      return;
    }
  }

  return new Response('Autenticação necessária', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="123Milhas Dashboard"' },
  });
}
