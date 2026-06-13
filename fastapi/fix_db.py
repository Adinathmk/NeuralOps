import asyncio
import re
from sqlalchemy import select
from app.database.session import AsyncSessionLocal
from app.models.incidents import Incident

async def main():
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Incident))
            incidents = result.scalars().all()
            for inc in incidents:
                fixed_frames = []
                changed = False
                for frame in inc.stack_frames:
                    if isinstance(frame, str):
                        changed = True
                        file_match = re.search(r"file='([^']+)'", frame)
                        line_match = re.search(r"line=(\d+)", frame)
                        method_match = re.search(r"method='([^']+)'", frame)
                        module_match = re.search(r"module='([^']+)'", frame)
                        fixed_frames.append({
                            "file": file_match.group(1) if file_match else "unknown",
                            "line": int(line_match.group(1)) if line_match else 0,
                            "method": method_match.group(1) if method_match else "unknown",
                            "module": module_match.group(1) if module_match else None
                        })
                    else:
                        fixed_frames.append(frame)
                
                if changed:
                    print(f"Fixing incident {inc.id}")
                    inc.stack_frames = fixed_frames
                    session.add(inc)
            
            await session.commit()
            print("DB fix complete.")
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
