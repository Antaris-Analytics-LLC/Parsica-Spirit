"""Memory compression & summarization."""

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List


class CompressionEngine:
    """Compress old memories into condensed summaries."""

    @staticmethod
    def compress_file(file_path: str) -> Dict:
        """Read a memory file and produce a compressed summary."""
        try:
            content = Path(file_path).read_text()
        except (FileNotFoundError, UnicodeDecodeError):
            return {"error": f"Cannot read: {file_path}"}

        lines = content.strip().split("\n")
        headers = [l for l in lines if l.startswith("#")]
        bullets = [l.strip() for l in lines if l.strip().startswith("- ")]
        markers = ["", "", "", "", "Decision:", "Key:", "Result:"]
        key_lines = [
            l.strip() for l in lines
            if any(m in l for m in markers)
        ]

        seen = set()
        unique = []
        for b in bullets + key_lines:
            norm = b.lower().strip("- ").strip()
            if norm not in seen and len(norm) > 10:
                seen.add(norm)
                unique.append(b)

        return {
            "source": file_path,
            "original_lines": len(lines),
            "compressed_lines": len(headers) + len(unique),
            "compression_ratio": round(
                1 - (len(headers) + len(unique)) / max(len(lines), 1), 2
            ),
            "headers": headers,
            "key_points": unique[:30],
            "compressed_at": datetime.now().isoformat(),
        }

    @staticmethod
    def compress_old_files(memory_dir: str, days_old: int = 7) -> List[Dict]:
        """Compress daily memory files older than *days_old*."""
        path = Path(memory_dir)
        if not path.exists():
            return []

        cutoff = datetime.now() - timedelta(days=days_old)
        results = []
        for f in sorted(path.glob("*.md")):
            match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            if not match:
                continue
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
            if file_date < cutoff:
                results.append(CompressionEngine.compress_file(str(f)))
        return results
