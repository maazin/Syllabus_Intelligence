import { provideHttpClient, withInterceptors } from '@angular/common/http';
import {
  ApplicationConfig,
  provideAppInitializer,
  provideZonelessChangeDetection,
} from '@angular/core';
import { provideRouter, withInMemoryScrolling } from '@angular/router';
import { routes } from './app.routes';
import { authInterceptor } from './core/auth.interceptor';
import { loadRuntimeConfig } from './core/runtime-config';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZonelessChangeDetection(),
    // Runs to completion before the first component renders, so no request
    // can fire against the fallback origin and then be silently retried
    // against the real one.
    provideAppInitializer(() => loadRuntimeConfig()),
    provideRouter(
      routes,
      // Returning to the top on navigation, but restoring position on back,
      // is what a student expects from any other site they use.
      withInMemoryScrolling({ scrollPositionRestoration: 'enabled', anchorScrolling: 'enabled' }),
    ),
    provideHttpClient(withInterceptors([authInterceptor])),
  ],
};
