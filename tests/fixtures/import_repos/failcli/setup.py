from setuptools import setup
setup(name="failcli", version="1.0", packages=["failcli"], description="Needs a data file it can't find.",
      entry_points={"console_scripts": ["failcli = failcli.cli:main"]})
