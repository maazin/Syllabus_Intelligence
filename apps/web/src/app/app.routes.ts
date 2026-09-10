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
