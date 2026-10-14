// SPDX-License-Identifier: MIT
/** Own asynchronous previews by selection, scope and immutable head. */
export class Preview {
  private generation = 0;
  invalidate(): void {
    this.generation++;
  }
  begin(scope: string): () => boolean {
    const generation = ++this.generation;
    return () => generation === this.generation && scope.length > 0;
  }
}
