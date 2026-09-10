import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { ApiService } from '../core/api.service';
import { AuthService } from '../core/auth.service';

/**
 * Sign-in (PRD section 15.2).
 *
 * Magic link only. There is no password to create, forget, or leak, and the
 * `.edu` address is the institution check, so the two requirements collapse
 * into one field.
 *
 * The "check your email" state says which address it went to, because during
 * syllabus week a typo in an address is the most likely reason nothing
 * arrives, and a student who cannot see what they typed will just wait.
 */
@Component({
  selector: 'si-signin',
  standalone: true,
  imports: [CommonModule, FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './signin.component.html',
  styleUrl: './signin.component.css',
})
export class SigninComponent {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  protected readonly email = signal('');
  protected readonly sent = signal(false);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);

  constructor() {
    // Arriving back from the emailed link: verify and continue.
    const token = this.route.snapshot.queryParamMap.get('token');
    if (token) this.verify(token);
  }

  protected submit(): void {
    const address = this.email().trim();
    if (!address) return;

    this.busy.set(true);
    this.error.set(null);

    this.api.requestMagicLink(address).subscribe({
      next: () => {
        this.sent.set(true);
        this.busy.set(false);
      },
      error: (response) => {
        this.error.set(
          response?.error?.detail ?? 'We could not send that link. Check the address and try again.',
        );
        this.busy.set(false);
      },
    });
  }

  private verify(token: string): void {
    this.busy.set(true);
    this.api.verifyMagicLink(token).subscribe({
      next: (response) => {
        this.auth.signIn(response.access_token, response.user);
        const next = this.route.snapshot.queryParamMap.get('next') ?? '/timeline';
        this.router.navigateByUrl(next);
      },
      error: () => {
        this.error.set('That link has expired. Request a new one below.');
        this.busy.set(false);
      },
    });
  }

  protected startOver(): void {
    this.sent.set(false);
    this.error.set(null);
  }
}
