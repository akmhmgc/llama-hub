"""
Github repository reader.

Retrieves the contents of a Github repository and returns a list of documents.
The documents are either the contents of the files in the repository or
the text extracted from the files using the parser.
"""
import asyncio
import base64
import binascii
import enum
import logging
import os
import pathlib
import tempfile
from typing import Any, Callable, List, Optional, Tuple

from llama_index.readers.base import BaseReader
from llama_index.readers.file.base import DEFAULT_FILE_EXTRACTOR
from llama_index.readers.llamahub_modules.github_repo.github_client import (
    BaseGithubClient,
    GitBranchResponseModel,
    GitCommitResponseModel,
    GithubClient,
    GitTreeResponseModel,
)
from llama_index.readers.llamahub_modules.github_repo.utils import (
    BufferedGitBlobDataIterator,
    get_file_extension,
    print_if_verbose,
)
from llama_index.readers.schema.base import Document

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class GithubRepositoryReader(BaseReader):
    """
    Github repository reader.

    Retrieves the contents of a Github repository and returns a list of documents.
    The documents are either the contents of the files in the repository or the text
    extracted from the files using the parser.

    Examples:
        >>> reader = GithubRepositoryReader("owner", "repo")
        >>> branch_documents = reader.load_data(branch="branch")
        >>> commit_documents = reader.load_data(commit_sha="commit_sha")

    """

    class FilterType(enum.Enum):
        """
        Filter type.

        Used to determine whether the filter is inclusive or exclusive.

        Attributes:
            - EXCLUDE: Exclude the files in the directories or with the extensions.
            - INCLUDE: Include only the files in the directories or with the extensions.
        """

        EXCLUDE = enum.auto()
        INCLUDE = enum.auto()

    def __init__(
        self,
        github_client: BaseGithubClient,
        owner: str,
        repo: str,
        use_parser: bool = True,
        verbose: bool = False,
        concurrent_requests: int = 5,
        filter_directories: Optional[Tuple[List[str], FilterType]] = None,
        filter_file_extensions: Optional[Tuple[List[str], FilterType]] = None,
    ):
        """
        Initialize params.

        Args:
            - github_client (BaseGithubClient): Github client.
            - owner (str): Owner of the repository.
            - repo (str): Name of the repository.
            - use_parser (bool): Whether to use the parser to extract
                the text from the files.
            - verbose (bool): Whether to print verbose messages.
            - concurrent_requests (int): Number of concurrent requests to
                make to the Github API.
            - filter_directories (Optional[Tuple[List[str], FilterType]]): Tuple
                containing a list of directories and a FilterType. If the FilterType
                is INCLUDE, only the files in the directories in the list will be
                included. If the FilterType is EXCLUDE, the files in the directories
                in the list will be excluded.
            - filter_file_extensions (Optional[Tuple[List[str], FilterType]]): Tuple
                containing a list of file extensions and a FilterType. If the
                FilterType is INCLUDE, only the files with the extensions in the list
                will be included. If the FilterType is EXCLUDE, the files with the
                extensions in the list will be excluded.

        Raises:
            - `ValueError`: If the github_token is not provided and
                the GITHUB_TOKEN environment variable is not set.
        """
        super().__init__()

        self._owner = owner
        self._repo = repo
        self._use_parser = use_parser
        self._verbose = verbose
        self._concurrent_requests = concurrent_requests
        self._filter_directories = filter_directories
        self._filter_file_extensions = filter_file_extensions

        # Set up the event loop
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # If there is no running loop, create a new one
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        self._github_client = github_client

    def _check_filter_directories(self, tree_obj_path: str) -> bool:
        """
        Check if a tree object should be allowed based on the directories.

        :param `tree_obj_path`: path of the tree object i.e. 'gpt_index/readers'

        :return: True if the tree object should be allowed, False otherwise
        """
        filter_directories, filter_type = self._filter_directories
        print_if_verbose(
            self._verbose,
            f"Checking {tree_obj_path} whether to {filter_type} it"
            + f" based on the filter directories: {filter_directories}",
        )

        if filter_type == self.FilterType.EXCLUDE:
            return not any(
                tree_obj_path.startswith(directory)
                or directory.startswith(tree_obj_path)
                for directory in filter_directories
            )
        elif filter_type == self.FilterType.INCLUDE:
            return any(
                tree_obj_path.startswith(directory)
                or directory.startswith(tree_obj_path)
                for directory in filter_directories
            )
        else:
            raise ValueError(
                f"Unknown filter type: {filter_type}. "
                "Please use either 'ignore' or 'include'."
            )

    def _check_filter_file_extensions(self, tree_obj_path: str) -> bool:
        """
        Check if a tree object should be allowed based on the file extensions.

        :param `tree_obj_path`: path of the tree object i.e. 'gpt_index/indices'

        :return: True if the tree object should be allowed, False otherwise
        """
        filter_file_extensions, filter_type = self._filter_file_extensions
        print_if_verbose(
            self._verbose,
            f"Checking {tree_obj_path} whether to {filter_type} it"
            + f" based on the filter file extensions: {filter_file_extensions}",
        )

        if filter_type == self.FilterType.EXCLUDE:
            return get_file_extension(tree_obj_path) not in filter_file_extensions
        elif filter_type == self.FilterType.INCLUDE:
            return get_file_extension(tree_obj_path) in filter_file_extensions
        else:
            raise ValueError(
                f"Unknown filter type: {filter_type}. "
                "Please use either 'ignore' or 'include'."
            )

    def _allow_tree_obj(self, tree_obj_path: str) -> bool:
        """
        Check if a tree object should be allowed.

        :param `tree_obj_path`: path of the tree object

        :return: True if the tree object should be allowed, False otherwise

        """
        if self._filter_directories is not None:
            return self._check_filter_directories(tree_obj_path)

        if self._filter_file_extensions is not None:
            return self._check_filter_file_extensions(tree_obj_path)

        return True

    def _load_data_from_commit(self, commit_sha: str) -> List[Document]:
        """
        Load data from a commit.

        Loads github repository data from a specific commit sha.

        :param `commit`: commit sha

        :return: list of documents
        """
        commit_response: GitCommitResponseModel = self._loop.run_until_complete(
            self._github_client.get_commit(self._owner, self._repo, commit_sha)
        )

        tree_sha = commit_response.commit.tree.sha
        blobs_and_paths = self._loop.run_until_complete(self._recurse_tree(tree_sha))

        print_if_verbose(self._verbose, f"got {len(blobs_and_paths)} blobs")

        return self._loop.run_until_complete(
            self._generate_documents(blobs_and_paths=blobs_and_paths)
        )

    def _load_data_from_branch(self, branch: str) -> List[Document]:
        """
        Load data from a branch.

        Loads github repository data from a specific branch.

        :param `branch`: branch name

        :return: list of documents
        """
        branch_data: GitBranchResponseModel = self._loop.run_until_complete(
            self._github_client.get_branch(self._owner, self._repo, branch)
        )

        tree_sha = branch_data.commit.commit.tree.sha
        blobs_and_paths = self._loop.run_until_complete(self._recurse_tree(tree_sha))

        print_if_verbose(self._verbose, f"got {len(blobs_and_paths)} blobs")

        return self._loop.run_until_complete(
            self._generate_documents(blobs_and_paths=blobs_and_paths)
        )

    def load_data(
        self,
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> List[Document]:
        """
        Load data from a commit or a branch.

        Loads github repository data from a specific commit sha or a branch.

        :param `commit`: commit sha
        :param `branch`: branch name

        :return: list of documents
        """
        if commit_sha is not None and branch is not None:
            raise ValueError("You can only specify one of commit or branch.")

        if commit_sha is None and branch is None:
            raise ValueError("You must specify one of commit or branch.")

        if commit_sha is not None:
            return self._load_data_from_commit(commit_sha)

        if branch is not None:
            return self._load_data_from_branch(branch)

        raise ValueError("You must specify one of commit or branch.")

    async def _recurse_tree(
        self, tree_sha: str, current_path: str = "", current_depth: int = 0
    ) -> Any:
        """
        Recursively get all blob tree objects in a tree.

        And construct their full path relative to the root of the repository.
        (see GitTreeResponseModel.GitTreeObject in
            github_api_client.py for more information)

        :param `tree_sha`: sha of the tree to recurse
        :param `current_path`: current path of the tree
        :param `current_depth`: current depth of the tree
        :return: list of tuples of
            (tree object, file's full path realtive to the root of the repo)
        """
        blobs_and_full_paths: List[Tuple[GitTreeResponseModel.GitTreeObject, str]] = []
        print_if_verbose(
            self._verbose, "\t" * current_depth + f"current path: {current_path}"
        )

        tree_data: GitTreeResponseModel = await self._github_client.get_tree(
            self._owner, self._repo, tree_sha
        )
        print_if_verbose(
            self._verbose, "\t" * current_depth + f"processing tree {tree_sha}"
        )
        for tree_obj in tree_data.tree:
            file_path = os.path.join(current_path, tree_obj.path)

            if tree_obj.type == "tree":
                print_if_verbose(
                    self._verbose,
                    "\t" * current_depth + f"recursing into {tree_obj.path}",
                )
                if not self._check_filter_directories(file_path):
                    print_if_verbose(
                        self._verbose,
                        "\t" * current_depth
                        + f"ignoring directory {tree_obj.path} due to filter",
                    )
                    continue

                blobs_and_full_paths.extend(
                    await self._recurse_tree(tree_obj.sha, file_path, current_depth + 1)
                )
            elif tree_obj.type == "blob":
                print_if_verbose(
                    self._verbose, "\t" * current_depth + f"found blob {tree_obj.path}"
                )
                if not self._check_filter_file_extensions(file_path):
                    print_if_verbose(
                        self._verbose,
                        "\t" * current_depth
                        + f"ignoring file {tree_obj.path} due to filter",
                    )
                    continue

                blobs_and_full_paths.append((tree_obj, file_path))
        return blobs_and_full_paths

    async def _generate_documents(
        self, blobs_and_paths: List[Tuple[GitTreeResponseModel.GitTreeObject, str]]
    ) -> List[Document]:
        """
        Generate documents from a list of blobs and their full paths.

        :param `blobs_and_paths`: list of tuples of
            (tree object, file's full path in the repo realtive to the root of the repo)
        :return: list of documents
        """
        buffered_iterator = BufferedGitBlobDataIterator(
            blobs_and_paths=blobs_and_paths,
            github_client=self._github_client,
            owner=self._owner,
            repo=self._repo,
            loop=self._loop,
            buffer_size=self._concurrent_requests,  # TODO: make this configurable
            verbose=self._verbose,
        )

        documents = []
        async for blob_data, full_path in buffered_iterator:
            print_if_verbose(self._verbose, f"generating document for {full_path}")
            assert (
                blob_data.encoding == "base64"
            ), f"blob encoding {blob_data.encoding} not supported"
            decoded_bytes = None
            try:
                decoded_bytes = base64.b64decode(blob_data.content)
                del blob_data.content
            except binascii.Error:
                print_if_verbose(
                    self._verbose, f"could not decode {full_path} as base64"
                )
                continue

            if self._use_parser:
                document = self._parse_supported_file(
                    file_path=full_path,
                    file_content=decoded_bytes,
                    tree_sha=blob_data.sha,
                    tree_path=full_path,
                )
                if document is not None:
                    documents.append(document)
                    continue
                print_if_verbose(
                    self._verbose,
                    f"could not parse {full_path} as a supported file type"
                    + " - falling back to decoding as utf-8 raw text",
                )

            try:
                if decoded_bytes is None:
                    raise ValueError("decoded_bytes is None")
                decoded_text = decoded_bytes.decode("utf-8")
            except UnicodeDecodeError:
                print_if_verbose(
                    self._verbose, f"could not decode {full_path} as utf-8"
                )
                continue
            print_if_verbose(
                self._verbose,
                f"got {len(decoded_text)} characters"
                + f"- adding to documents - {full_path}",
            )
            document = Document(
                text=decoded_text,
                doc_id=blob_data.sha,
                extra_info={
                    "file_path": full_path,
                    "file_name": full_path.split("/")[-1],
                },
            )
            documents.append(document)
        return documents

    def _parse_supported_file(
        self, file_path: str, file_content: bytes, tree_sha: str, tree_path: str
    ) -> Optional[Document]:
        """
        Parse a file if it is supported by a parser.

        :param `file_path`: path of the file in the repo
        :param `file_content`: content of the file
        :return: Document if the file is supported by a parser, None otherwise
        """
        file_extension = get_file_extension(file_path)
        if (parser := DEFAULT_FILE_EXTRACTOR.get(file_extension)) is not None:
            parser.init_parser()
            print_if_verbose(
                self._verbose,
                f"parsing {file_path}"
                + f"as {file_extension} with "
                + f"{parser.__class__.__name__}",
            )
            with tempfile.TemporaryDirectory() as tmpdirname:
                with tempfile.NamedTemporaryFile(
                    dir=tmpdirname,
                    suffix=f".{file_extension}",
                    mode="w+b",
                    delete=False,
                ) as tmpfile:
                    print_if_verbose(
                        self._verbose,
                        "created a temporary file"
                        + f"{tmpfile.name} for parsing {file_path}",
                    )
                    tmpfile.write(file_content)
                    tmpfile.flush()
                    tmpfile.close()
                    try:
                        parsed_file = parser.parse_file(pathlib.Path(tmpfile.name))
                        parsed_file = "\n\n".join(parsed_file)
                    except Exception as e:
                        print_if_verbose(
                            self._verbose, f"error while parsing {file_path}"
                        )
                        logger.error(
                            "Error while parsing "
                            + f"{file_path} with "
                            + f"{parser.__class__.__name__}:\n{e}"
                        )
                        parsed_file = None
                    finally:
                        os.remove(tmpfile.name)
                    if parsed_file is None:
                        return None
                    return Document(
                        text=parsed_file,
                        doc_id=tree_sha,
                        extra_info={
                            "file_path": file_path,
                            "file_name": tree_path,
                        },
                    )
        return None


if __name__ == "__main__":
    import time

    def timeit(func: Callable) -> Callable:
        """Time a function."""

        def wrapper(*args: Any, **kwargs: Any) -> None:
            """Callcuate time taken to run a function."""
            start = time.time()
            func(*args, **kwargs)
            end = time.time()
            print(f"Time taken: {end - start} seconds for {func.__name__}")

        return wrapper

    github_client = GithubClient(github_token=os.environ["GITHUB_TOKEN"], verbose=True)

    reader1 = GithubRepositoryReader(
        github_client=github_client,
        owner="jerryjliu",
        repo="gpt_index",
        use_parser=False,
        verbose=True,
        filter_directories=(
            ["docs"],
            GithubRepositoryReader.FilterType.INCLUDE,
        ),
        filter_file_extensions=(
            [".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", "json", ".ipynb"],
            GithubRepositoryReader.FilterType.EXCLUDE,
        ),
    )

    @timeit
    def load_data_from_commit() -> None:
        """Load data from a commit."""
        documents = reader1.load_data(
            commit_sha="22e198b3b166b5facd2843d6a62ac0db07894a13"
        )
        for document in documents:
            print(document.extra_info)

    @timeit
    def load_data_from_branch() -> None:
        """Load data from a branch."""
        documents = reader1.load_data(branch="main")
        for document in documents:
            print(document.extra_info)

    input("Press enter to load github repository from branch name...")

    load_data_from_branch()

    # input("Press enter to load github repository from commit sha...")

    # load_data_from_commit()
