import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { AuthService } from './core/auth.service';

/**
 * App shell.
 *
 * Navigation follows the platform convention for each form factor: a top bar
 * on desktop where horizontal space is cheap, and a bottom bar on small
 * screens where the top of a tall phone is out of thumb reach. Both render
 * the same three destinations in the same order, so the mental model does not
 * change when the window does.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, RouterOutlet, RouterLink, RouterLinkActive],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  protected signOut(): void {
    this.auth.signOut();
    this.router.navigate(['/signin']);
  }
}
