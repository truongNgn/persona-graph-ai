"""Legacy entry point: use smoke_local_llm.py for either local engine."""
import asyncio
from smoke_local_llm import main

if __name__ == "__main__":
    asyncio.run(main())
