import { Routes } from '@angular/router';
import { authGuard } from './core/auth.guard';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'timeline' },
  {
    path: 'signin',
    loadComponent: () => import('./signin/signin.component').then((m) => m.SigninComponent),
    title: 'Sign in',
  },
  {
    // The path the emailed link uses. Same screen: it reads `?token=` and
    // verifies. Kept as its own route rather than a redirect so the token is
    // never dropped in a hop, and so an old link in someone's inbox still
    // works if the canonical path changes again.
    path: 'auth/verify',
    loadComponent: () => import('./signin/signin.component').then((m) => m.SigninComponent),
    title: 'Signing you in',
  },
  {
    path: 'timeline',
    canActivate: [authGuard],
    loadComponent: () => import('./timeline/timeline.component').then((m) => m.TimelineComponent),
    title: 'Your semester',
  },
  {
    path: 'upload',
    canActivate: [authGuard],
    loadComponent: () => import('./upload/upload.component').then((m) => m.UploadComponent),
    title: 'Add a syllabus',
  },
  {
    path: 'review/:documentId',
    canActivate: [authGuard],
    loadComponent: () => import('./review/review.component').then((m) => m.ReviewComponent),
    title: 'Check your deadlines',
  },
  {
    path: 'search',
    canActivate: [authGuard],
    loadComponent: () => import('./search/search.component').then((m) => m.SearchComponent),
    title: 'Find a course',
  },
  { path: '**', redirectTo: 'timeline' },
];
