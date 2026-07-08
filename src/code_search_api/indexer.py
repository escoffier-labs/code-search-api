"""Run the local indexer directly without HTTP."""

from .server import init_db, migrate_db, perform_index


def main():
    init_db()
    migrate_db()

    print("Collecting files...")
    result = perform_index(summarize=False)

    print(f"\n{'='*50}")
    print("Index complete!")
    print(f"  Files scanned: {result['files_found']}")
    print(f"  New files: {result['files_new']}")
    print(f"  Changed files: {result['files_changed']}")
    print(f"  Skipped files: {result['files_skipped']}")
    print(f"  Metadata refreshed: {result['files_refreshed']}")
    print(f"  New/updated chunks: {result['new_chunks']}")
    print(f"  Skipped chunks: {result['skipped_unchanged']}")
    print(f"  Embedded: {result['embedded']}")
    print(f"  Failed: {result['failed']}")
    print(f"  Time: {result['duration_seconds'] / 60:.1f} minutes")


if __name__ == "__main__":
    main()
