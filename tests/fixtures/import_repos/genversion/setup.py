from setuptools import setup
from setuptools.command.build_py import build_py


class WriteVersion(build_py):
    def run(self):
        super().run()
        with open(self.build_lib + "/genversion/_version.py", "w") as fh:
            fh.write("version = '2.1.0'\n")


setup(name="genversion", version="2.1.0", packages=["genversion"], cmdclass={"build_py": WriteVersion},
      description="Doubles numbers; its version module is generated when the package is built.")
