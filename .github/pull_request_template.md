### What this PR changes
- 

### Steps to review
1. Check out the branch.
2. Install: `pip install -e ".[memory,test]" numpy` (inside a virtualenv).
3. Lint: `ruff check .`
4. Run the suite: `FLEETWATCH_NO_MIGRATE=1 python -m unittest discover -s tests`
5. Describe how to see the change running (the command, the screen, what you should see).

### Checklist
- [ ] Tests cover the change.
- [ ] Fixtures, docs and examples use fictional names and paths only (no real people, home directories, email addresses or cloud-drive paths).
- [ ] No credentials or tokens in the diff.
