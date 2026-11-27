from setuptools import setup
setup(name="pycli", version="1.0", packages=["pycli"], description="Shout text from the command line.",
      entry_points={"console_scripts": ["pycli = pycli.cli:main"]})
