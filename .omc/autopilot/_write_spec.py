import pathlib, sys
spec_path = pathlib.Path(sys.argv[1])
spec_path.write_text(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"), encoding="utf-8")
print(f"Written {spec_path.stat().st_size} bytes")
