""" handle user uploads into browsertrix """

import uuid
import hashlib
import os
import base64

from io import BufferedReader
from typing import Optional, List
from fastapi import Depends, UploadFile, File

from fastapi import HTTPException
from pydantic import Field, UUID4

from starlette.requests import Request
from pathvalidate import sanitize_filename

from .basecrawls import (
    BaseCrawl,
    BaseCrawlOut,
    BaseCrawlOutWithResources,
    BaseCrawlOps,
    CrawlFile,
    UpdateCrawl,
    DeleteCrawlList,
)
from .users import User
from .orgs import Organization
from .pagination import PaginatedResponseModel, paginated_format, DEFAULT_PAGE_SIZE
from .storages import do_upload_single, do_upload_multipart
from .utils import dt_now


MIN_UPLOAD_PART_SIZE = 10000000


# ============================================================================
class UploadedCrawl(BaseCrawl):
    """Store State of a Crawl Upload"""

    type: str = Field("upload", const=True)

    name: str


# ============================================================================
class UploadedCrawlOut(BaseCrawlOut):
    """Output model for Crawl Uploads"""


# ============================================================================
class UploadedCrawlOutWithResources(BaseCrawlOutWithResources):
    """Output model for Crawl Uploads with all file resources"""


# ============================================================================
class UpdateUpload(UpdateCrawl):
    """Update modal that also includes name"""

    name: Optional[str]


# ============================================================================
class UploadOps(BaseCrawlOps):
    """upload ops"""

    # pylint: disable=too-many-arguments, too-many-locals, duplicate-code, invalid-name
    async def upload_stream(
        self,
        stream,
        filename: str,
        name: Optional[str],
        notes: Optional[str],
        org: Organization,
        user: User,
        replaceId: Optional[str],
    ):
        """Upload streaming file, length unknown"""

        prev_upload = None
        if replaceId:
            try:
                prev_upload = await self.get_crawl_raw(replaceId, org, "upload")
            except HTTPException:
                # not found
                replaceId = None

        id_ = "upload-" + str(uuid.uuid4()) if not replaceId else replaceId

        prefix = f"{org.id}/uploads/{id_}/"
        file_prep = FilePreparer(prefix, filename)

        async def stream_iter():
            """iterate over each chunk and compute and digest + total size"""
            async for chunk in stream:
                file_prep.add_chunk(chunk)
                yield chunk

        print("Stream Upload Start", flush=True)

        if not await do_upload_multipart(
            org,
            file_prep.upload_name,
            stream_iter(),
            MIN_UPLOAD_PART_SIZE,
            self.crawl_manager,
        ):
            print("Stream Upload Failed", flush=True)
            raise HTTPException(status_code=400, detail="upload_failed")

        files = [file_prep.get_crawl_file()]

        if prev_upload:
            try:
                await self._delete_crawl_files(prev_upload, org)
            # pylint: disable=broad-exception-caught
            except Exception as exc:
                print("replace file deletion failed", exc)

        return await self._create_upload(files, name, notes, id_, org, user)

    # pylint: disable=too-many-arguments, too-many-locals
    async def upload_formdata(
        self,
        uploads: List[UploadFile],
        name: Optional[str],
        notes: Optional[str],
        org: Organization,
        user: User,
    ):
        """handle uploading content to uploads subdir + request subdir"""
        id_ = uuid.uuid4()
        files = []
        prefix = f"{org.id}/uploads/{id_}/"

        for upload in uploads:
            file_prep = FilePreparer(prefix, upload.filename)
            file_reader = UploadFileReader(upload, file_prep)

            await do_upload_single(
                org, file_reader.file_prep.upload_name, file_reader, self.crawl_manager
            )
            files.append(file_reader.file_prep.get_crawl_file())

        return await self._create_upload(files, name, notes, id_, org, user)

    async def _create_upload(self, files, name, notes, id_, org, user):
        now = dt_now()
        # ts_now = now.strftime("%Y%m%d%H%M%S")
        # crawl_id = f"upload-{ts_now}-{str(id_)[:12]}"
        crawl_id = str(id_)

        file_size = sum(file_.size for file_ in files)

        uploaded = UploadedCrawl(
            id=crawl_id,
            name=name or "New Upload @ " + str(now),
            notes=notes,
            userid=user.id,
            oid=org.id,
            files=files,
            state="complete",
            fileCount=len(files),
            fileSize=file_size,
            started=now,
            finished=now,
        )

        # result = await self.crawls.insert_one(uploaded.to_dict())
        # return {"id": str(result.inserted_id), "added": True}
        await self.crawls.find_one_and_update(
            {"_id": crawl_id}, {"$set": uploaded.to_dict()}, upsert=True
        )
        return {"id": crawl_id, "added": True}

    async def delete_uploads(
        self, delete_list: DeleteCrawlList, org: Optional[Organization] = None
    ):
        """Delete uploaded crawls"""
        deleted_count, _, _ = await self.delete_crawls(org, delete_list, "upload")

        if deleted_count < 1:
            raise HTTPException(status_code=404, detail="uploaded_crawl_not_found")

        return {"deleted": True}


# ============================================================================
class FilePreparer:
    """wrapper to compute digest / name for streaming upload"""

    def __init__(self, prefix, filename):
        self.upload_size = 0
        self.upload_hasher = hashlib.sha256()
        self.upload_name = prefix + self.prepare_filename(filename)

    def add_chunk(self, chunk):
        """add chunk for file"""
        self.upload_size += len(chunk)
        self.upload_hasher.update(chunk)

    def get_crawl_file(self, def_storage_name="default"):
        """get crawl file"""
        return CrawlFile(
            filename=self.upload_name,
            hash=self.upload_hasher.hexdigest(),
            size=self.upload_size,
            def_storage_name=def_storage_name,
        )

    def prepare_filename(self, filename):
        """prepare filename by sanitizing and adding extra string
        to avoid duplicates"""
        name = sanitize_filename(filename.rsplit("/", 1)[-1])
        parts = name.split(".")
        randstr = base64.b32encode(os.urandom(5)).lower()
        parts[0] += "-" + randstr.decode("utf-8")
        return ".".join(parts)


# ============================================================================
class UploadFileReader(BufferedReader):
    """Compute digest on file upload"""

    def __init__(self, upload, file_prep):
        super().__init__(upload.file._file)
        self.file_prep = file_prep

    def read(self, size, *args):
        """read and digest file chunk"""
        chunk = super().read(size, *args)
        self.file_prep.add_chunk(chunk)
        return chunk


# ============================================================================
# pylint: disable=too-many-arguments, too-many-locals, invalid-name
def init_uploads_api(app, mdb, users, crawl_manager, orgs, user_dep):
    """uploads api"""

    # ops = CrawlOps(mdb, users, crawl_manager, crawl_config_ops, orgs)
    ops = UploadOps(mdb, users, crawl_manager)

    org_viewer_dep = orgs.org_viewer_dep
    org_crawl_dep = orgs.org_crawl_dep

    @app.put("/orgs/{oid}/uploads/formdata", tags=["uploads"])
    async def upload_formdata(
        uploads: List[UploadFile] = File(...),
        name: Optional[str] = "",
        notes: Optional[str] = "",
        org: Organization = Depends(org_crawl_dep),
        user: User = Depends(user_dep),
    ):
        return await ops.upload_formdata(uploads, name, notes, org, user)

    @app.put("/orgs/{oid}/uploads/stream", tags=["uploads"])
    async def upload_stream(
        request: Request,
        filename: str,
        name: Optional[str] = "",
        notes: Optional[str] = "",
        replaceId: Optional[str] = "",
        org: Organization = Depends(org_crawl_dep),
        user: User = Depends(user_dep),
    ):
        return await ops.upload_stream(
            request.stream(), filename, name, notes, org, user, replaceId
        )

    @app.get(
        "/orgs/{oid}/uploads", tags=["uploads"], response_model=PaginatedResponseModel
    )
    async def list_uploads(
        org: Organization = Depends(org_viewer_dep),
        pageSize: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
        userid: Optional[UUID4] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        collectionId: Optional[UUID4] = None,
        sortBy: Optional[str] = "finished",
        sortDirection: Optional[int] = -1,
    ):
        uploads, total = await ops.list_all_base_crawls(
            org,
            userid=userid,
            name=name,
            description=description,
            page_size=pageSize,
            page=page,
            collection_id=collectionId,
            sort_by=sortBy,
            sort_direction=sortDirection,
            type_="upload",
            cls_type=UploadedCrawlOut,
        )
        return paginated_format(uploads, total, page, pageSize)

    @app.get(
        "/orgs/{oid}/uploads/{crawlid}",
        tags=["uploads"],
        response_model=UploadedCrawlOutWithResources,
    )
    async def get_upload(crawlid: str, org: Organization = Depends(org_crawl_dep)):
        res = await ops.get_resource_resolved_raw_crawl(crawlid, org, "upload")
        return UploadedCrawlOutWithResources.from_dict(res)

    @app.get(
        "/orgs/all/uploads/{crawl_id}/replay.json",
        tags=["uploads"],
        response_model=BaseCrawlOutWithResources,
    )
    async def get_upload_replay_admin(crawl_id, user: User = Depends(user_dep)):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")

        return await ops.get_crawl(crawl_id, None, "upload")

    @app.get(
        "/orgs/{oid}/uploads/{crawl_id}/replay.json",
        tags=["uploads"],
        response_model=BaseCrawlOutWithResources,
    )
    async def get_upload_replay(crawl_id, org: Organization = Depends(org_viewer_dep)):
        return await ops.get_crawl(crawl_id, org, "upload")

    @app.patch("/orgs/{oid}/uploads/{crawl_id}", tags=["uploads"])
    async def update_uploads_api(
        update: UpdateUpload, crawl_id: str, org: Organization = Depends(org_crawl_dep)
    ):
        return await ops.update_crawl(crawl_id, org, update, "upload")

    @app.post("/orgs/{oid}/uploads/delete", tags=["uploads"])
    async def delete_uploads(
        delete_list: DeleteCrawlList,
        org: Organization = Depends(org_crawl_dep),
    ):
        return await ops.delete_uploads(delete_list, org)
