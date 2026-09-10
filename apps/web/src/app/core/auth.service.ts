import { Injectable, computed, signal } from '@angular/core';
import { User } from './models';

const TOKEN_KEY = 'si.token';
const USER_KEY = 'si.user';

/** Session state.
 *
 * The token lives in localStorage so a student who closes the tab mid-term
 * does not have to re-authenticate through email every time. That is a
 * deliberate trade: magic-link auth has no password to re-enter, so an
 * expired session means waiting on an email, which during syllabus week is
 * exactly the friction that loses an activation.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly _user = signal<User | null>(this.restoreUser());
  private readonly _token = signal<string | null>(localStorage.getItem(TOKEN_KEY));

  readonly user = this._user.asReadonly();
  readonly isAuthenticated = computed(() => this._token() !== null);

  get token(): string | null {
    return this._token();
  }

  signIn(token: string, user: User): void {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
    this._token.set(token);
    this._user.set(user);
  }

  signOut(): void {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    this._token.set(null);
    this._user.set(null);
  }

  private restoreUser(): User | null {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as User;
    } catch {
      // A corrupted entry should log the student out cleanly rather than
      // leaving the app in a half-authenticated state.
      localStorage.removeItem(USER_KEY);
      return null;
    }
  }
}
