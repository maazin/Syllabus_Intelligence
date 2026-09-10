import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { ApiService } from '../core/api.service';
import { UploadResult } from '../core/models';

interface FileRow {
  file: File;
  status: 'pending' | 'uploading' | 'queued' | 'ready' | 'failed';
  message: string | null;
  documentId: string | null;
}

/**
 * Upload (PRD section 6.1, US-1).
 *
 * The requirement that shapes this screen is that one file failing must never
 * block the other four. Each file therefore carries its own row and its own
 * status, and a failure is reported next to the file that caused it rather
 * than as a single banner that hides which upload actually broke.
 *
 * A deduplicated file reports "ready" instead of "queued": someone in the same
 * section already uploaded these exact bytes, so the parse exists and this
 * student skips the wait entirely.
 */
@Component({
  selector: 'si-upload',
  standalone: true,
  imports: [CommonModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './upload.component.html',
  styleUrl: './upload.component.css',
})
export class UploadComponent {
  private readonly api = inject(ApiService);
  private readonly router = inject(Router);

  protected readonly MAX_FILES = 10;
  protected readonly MAX_MB = 25;

  protected readonly rows = signal<FileRow[]>([]);
  protected readonly dragging = signal(false);
  protected readonly uploading = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly accepted = '.pdf,.docx,.png,.jpg,.jpeg,.heic,.txt,.html';

  protected readonly canUpload = computed(
    () => this.rows().some((row) => row.status === 'pending') && !this.uploading(),
  );

  protected readonly finished = computed(() =>
    this.rows().filter((row) => row.status === 'queued' || row.status === 'ready'),
  );

  protected readonly allDone = computed(
    () => this.rows().length > 0 && this.rows().every((row) => row.status !== 'pending'),
  );

  protected onDragOver(event: DragEvent): void {
    event.preventDefault();
    this.dragging.set(true);
  }

  protected onDragLeave(): void {
    this.dragging.set(false);
  }

  protected onDrop(event: DragEvent): void {
    event.preventDefault();
    this.dragging.set(false);
    this.addFiles(Array.from(event.dataTransfer?.files ?? []));
  }

  protected onPicked(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.addFiles(Array.from(input.files ?? []));
    // Clear so picking the same file twice still fires a change event.
    input.value = '';
  }

  private addFiles(files: File[]): void {
    if (files.length === 0) return;
    this.error.set(null);

    const existing = this.rows();
    const room = this.MAX_FILES - existing.length;
    if (room <= 0) {
      this.error.set(`You can upload ${this.MAX_FILES} files at a time.`);
      return;
    }

    const accepted = files.slice(0, room).map<FileRow>((file) => {
      const tooBig = file.size > this.MAX_MB * 1024 * 1024;
      return {
        file,
        status: tooBig ? 'failed' : 'pending',
        message: tooBig ? `Larger than ${this.MAX_MB} MB` : null,
        documentId: null,
      };
    });

    this.rows.set([...existing, ...accepted]);

    if (files.length > room) {
      this.error.set(
        `We added the first ${room}. You can upload ${this.MAX_FILES} files at a time.`,
      );
    }
  }

  protected remove(index: number): void {
    this.rows.update((rows) => rows.filter((_, i) => i !== index));
  }

  protected upload(): void {
    const pending = this.rows().filter((row) => row.status === 'pending');
    if (pending.length === 0) return;

    this.uploading.set(true);
    this.markStatus(pending, 'uploading');

    this.api.uploadDocuments(pending.map((row) => row.file)).subscribe({
      next: (results) => {
        this.applyResults(results);
        this.uploading.set(false);
      },
      error: () => {
        this.markStatus(pending, 'failed', 'Upload did not go through. Try again.');
        this.uploading.set(false);
      },
    });
  }

  private markStatus(targets: FileRow[], status: FileRow['status'], message?: string): void {
    const names = new Set(targets.map((row) => row.file.name));
    this.rows.update((rows) =>
      rows.map((row) =>
        names.has(row.file.name)
          ? { ...row, status, message: message ?? row.message }
          : row,
      ),
    );
  }

  private applyResults(results: UploadResult[]): void {
    const byName = new Map(results.map((result) => [result.filename, result]));
    this.rows.update((rows) =>
      rows.map((row) => {
        const result = byName.get(row.file.name);
        if (!result) return row;
        if (result.status === 'failed') {
          return { ...row, status: 'failed', message: result.error ?? 'We could not read this file.' };
        }
        return {
          ...row,
          status: result.deduped ? 'ready' : 'queued',
          documentId: result.document_id,
          message: result.deduped
            ? 'Someone in your section already uploaded this, so it is ready now.'
            : 'Reading your syllabus. This usually takes under a minute.',
        };
      }),
    );
  }

  protected review(): void {
    const first = this.finished()[0];
    if (first?.documentId) {
      this.router.navigate(['/review', first.documentId]);
    }
  }

  protected sizeLabel(bytes: number): string {
    const mb = bytes / (1024 * 1024);
    return mb < 0.1 ? `${Math.round(bytes / 1024)} KB` : `${mb.toFixed(1)} MB`;
  }
}
