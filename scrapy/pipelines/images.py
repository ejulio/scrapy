"""
Images Pipeline

See documentation in topics/media-pipeline.rst
"""
import functools
import hashlib
import six

try:
    from cStringIO import StringIO as BytesIO
except ImportError:
    from io import BytesIO

from PIL import Image

from scrapy.utils.misc import md5sum
from scrapy.utils.python import to_bytes
from scrapy.http import Request
from scrapy.settings import Settings
from scrapy.exceptions import DropItem
#TODO: from scrapy.pipelines.media import MediaPipeline
from scrapy.pipelines.files import FileException, FilesPipeline


class NoimagesDrop(DropItem):
    """Product with no images exception"""


class ImageException(FileException):
    """General image error exception"""


class ImagesPipeline(FilesPipeline):
    """Abstract pipeline that implement the image thumbnail generation logic

    """

    MEDIA_NAME = 'image'

    # Uppercase attributes kept for backward compatibility with code that subclasses
    # ImagesPipeline. They may be overridden by settings.
    MIN_WIDTH = 0
    MIN_HEIGHT = 0
    EXPIRES = 90
    THUMBS = {}
    DEFAULT_IMAGES_URLS_FIELD = 'image_urls'
    DEFAULT_IMAGES_RESULT_FIELD = 'images'

    def __init__(self, store_uri, download_func=None, settings=None):
        super(ImagesPipeline, self).__init__(store_uri, settings=settings,
                                             download_func=download_func)

        if isinstance(settings, dict) or settings is None:
            settings = Settings(settings)

        resolve = functools.partial(self._key_for_pipe,
                                    base_class_name="ImagesPipeline",
                                    settings=settings)
        self.expires = settings.getint(
            resolve("IMAGES_EXPIRES"), self.EXPIRES
        )

        if not hasattr(self, "IMAGES_RESULT_FIELD"):
            self.IMAGES_RESULT_FIELD = self.DEFAULT_IMAGES_RESULT_FIELD
        if not hasattr(self, "IMAGES_URLS_FIELD"):
            self.IMAGES_URLS_FIELD = self.DEFAULT_IMAGES_URLS_FIELD

        self.images_urls_field = settings.get(
            resolve('IMAGES_URLS_FIELD'),
            self.IMAGES_URLS_FIELD
        )
        self.images_result_field = settings.get(
            resolve('IMAGES_RESULT_FIELD'),
            self.IMAGES_RESULT_FIELD
        )
        self.min_width = settings.getint(
            resolve('IMAGES_MIN_WIDTH'), self.MIN_WIDTH
        )
        self.min_height = settings.getint(
            resolve('IMAGES_MIN_HEIGHT'), self.MIN_HEIGHT
        )
        self.thumbs = settings.get(
            resolve('IMAGES_THUMBS'), self.THUMBS
        )

    @classmethod
    def from_settings(cls, settings):
        s3store = cls.STORAGES['s3']
        s3store.AWS_ACCESS_KEY_ID = settings['AWS_ACCESS_KEY_ID']
        s3store.AWS_SECRET_ACCESS_KEY = settings['AWS_SECRET_ACCESS_KEY']
        s3store.AWS_ENDPOINT_URL = settings['AWS_ENDPOINT_URL']
        s3store.AWS_REGION_NAME = settings['AWS_REGION_NAME']
        s3store.AWS_USE_SSL = settings['AWS_USE_SSL']
        s3store.AWS_VERIFY = settings['AWS_VERIFY']
        s3store.POLICY = settings['IMAGES_STORE_S3_ACL']

        gcs_store = cls.STORAGES['gs']
        gcs_store.GCS_PROJECT_ID = settings['GCS_PROJECT_ID']
        gcs_store.POLICY = settings['IMAGES_STORE_GCS_ACL'] or None

        store_uri = settings['IMAGES_STORE']
        return cls(store_uri, settings=settings)

    def file_downloaded(self, response, request, info):
        return self.image_downloaded(response, request, info)

    def image_downloaded(self, response, request, info):
        checksum = None
        for path, image, buf in self.get_images(response, request, info):
            if checksum is None:
                buf.seek(0)
                checksum = md5sum(buf)
            width, height = image.size
            self.store.persist_file(
                path, buf, info,
                meta={'width': width, 'height': height},
                headers={'Content-Type': 'image/jpeg'})
        return checksum

    def get_images(self, response, request, info):
        path = self.file_path(request, response=response, info=info)
        orig_image = Image.open(BytesIO(response.body))

        width, height = orig_image.size
        if width < self.min_width or height < self.min_height:
            raise ImageException("Image too small (%dx%d < %dx%d)" %
                                 (width, height, self.min_width, self.min_height))

        image, buf = self.convert_image(orig_image)
        yield path, image, buf

        for thumb_id, size in six.iteritems(self.thumbs):
            thumb_path = self.thumb_path(request, thumb_id, response=response, info=info)
            thumb_image, thumb_buf = self.convert_image(image, size)
            yield thumb_path, thumb_image, thumb_buf

    def convert_image(self, image, size=None):
        if image.format == 'PNG' and image.mode == 'RGBA':
            background = Image.new('RGBA', image.size, (255, 255, 255))
            background.paste(image, image)
            image = background.convert('RGB')
        elif image.mode == 'P':
            image = image.convert("RGBA")
            background = Image.new('RGBA', image.size, (255, 255, 255))
            background.paste(image, image)
            image = background.convert('RGB')
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        if size:
            image = image.copy()
            image.thumbnail(size, Image.ANTIALIAS)

        buf = BytesIO()
        image.save(buf, 'JPEG')
        return image, buf

    def get_media_requests(self, item, info):
        return [Request(x) for x in item.get(self.images_urls_field, [])]

    def item_completed(self, results, item, info):
        if isinstance(item, dict) or self.images_result_field in item.fields:
            item[self.images_result_field] = [x for ok, x in results if ok]
        return item

    def file_path(self, request, response=None, info=None):
        image_guid = hashlib.sha1(to_bytes(request.url)).hexdigest()
        return 'full/%s.jpg' % (image_guid)

    def thumb_path(self, request, thumb_id, response=None, info=None):
        thumb_guid = hashlib.sha1(to_bytes(request.url)).hexdigest()
        return 'thumbs/%s/%s.jpg' % (thumb_id, thumb_guid)
