import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';
import { AuthService } from './auth.service';
import { runtimeConfig } from './runtime-config';

/** Attaches the bearer token and handles expiry in one place.
 *
 * A 401 means the token is gone or invalid, so the session is cleared and the
 * student is sent to sign-in. Leaving a dead token in storage would produce a
 * confusing loop of empty screens instead of a clear prompt.
 *
 * The token goes only to the configured API base. In development that base is
 * a relative path and every request is same-origin anyway, but in production
 * it is an absolute origin, and an interceptor that attaches a credential to
 * every outgoing request would hand a student's session token to any host the
 * app ever fetches from.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const token = auth.token;

  const request =
    token && isApiRequest(req.url)
      ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } })
      : req;

  return next(request).pipe(
    catchError((error: HttpErrorResponse) => {
      if (error.status === 401 && !req.url.includes('/auth/')) {
        auth.signOut();
        router.navigate(['/signin']);
      }
      return throwError(() => error);
    }),
  );
};

/** True when the URL is under the configured API base.
 *
 * Both sides are resolved against the document origin so a relative base and
 * an absolute request URL compare correctly, which is what happens the moment
 * the API moves to its own subdomain.
 */
function isApiRequest(url: string): boolean {
  const origin = typeof location === 'undefined' ? 'http://localhost' : location.origin;
  try {
    const base = new URL(runtimeConfig().apiBase, origin);
    const target = new URL(url, origin);
    return target.origin === base.origin && target.pathname.startsWith(base.pathname);
  } catch {
    return false;
  }
}
