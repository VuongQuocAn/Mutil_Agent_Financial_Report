import argparse
from pathlib import Path

from financial_multiagent.common.artifacts import canonical_json
from .contracts import C1Profile
from .runner import C1Input, run_c1


def main():
    parser = argparse.ArgumentParser(description="C1 validation of an explicitly selected normalization run")
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--normalization-dir", type=Path, required=True)
    parser.add_argument("--access-scope", required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--absolute-tolerance", default="0")
    args = parser.parse_args()
    result = run_c1([C1Input(args.bundle_dir, args.normalization_dir)], access_scope=args.access_scope,
        output_dir=args.output_dir, registry_path=args.registry, context_path=args.context,
        profile=C1Profile(absolute_tolerance=args.absolute_tolerance))
    print(canonical_json({k: result[k] for k in ("execution_status", "quality_status", "legal_status", "outcome_counts", "output_dir")}))


if __name__ == "__main__":
    main()
