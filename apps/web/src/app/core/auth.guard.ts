import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService } from './auth.service';

export const authGuard: CanActivateFn = (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);

  if (auth.isAuthenticated()) return true;

  // Carry the intended destination so sign-in can return the student there
  // rather than dropping them on a generic landing page.
  return router.createUrlTree(['/signin'], { queryParams: { next: state.url } });
};
