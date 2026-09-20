// Optional HEIC/HEIF decoding through libheif (see Heif.h).
#include "Heif.h"
#include <QColorSpace>
#include <QFile>
#include <QTransform>

#ifdef COMPOSITOR_HAVE_LIBHEIF
#include <libheif/heif.h>
#include <cstring>
#include <vector>
#endif

namespace app {

#ifdef COMPOSITOR_HAVE_LIBHEIF
namespace {

// EXIF orientation (tag 0x0112) out of the raw metadata libheif hands back; the
// payload is a TIFF header, sometimes behind an "Exif\0\0" prefix and a 4-byte
// offset. Returns 1 (normal) when there is nothing to read.
int exifOrientation(const std::vector<uint8_t>& data) {
    if (data.size() < 8) return 1;
    size_t start = 0;
    for (size_t i = 0; i + 6 <= data.size(); i++)
        if (std::memcmp(&data[i], "Exif\0\0", 6) == 0) { start = i + 6; break; }
    // Some writers put a 4-byte offset before the TIFF header.
    bool little = false;
    const uint8_t* tiff = nullptr;
    for (size_t i = start; i + 8 <= data.size(); i++) {
        if (std::memcmp(&data[i], "II\x2a\x00", 4) == 0) { little = true; tiff = &data[i]; break; }
        if (std::memcmp(&data[i], "MM\x00\x2a", 4) == 0) { little = false; tiff = &data[i]; break; }
    }
    if (!tiff) return 1;
    auto u16 = [little](const uint8_t* p) { return little ? uint16_t(p[0] | (p[1] << 8)) : uint16_t((p[0] << 8) | p[1]); };
    auto u32 = [little](const uint8_t* p) {
        return little ? uint32_t(p[0] | (p[1] << 8) | (p[2] << 16) | (uint32_t(p[3]) << 24))
                      : uint32_t((uint32_t(p[0]) << 24) | (p[1] << 16) | (p[2] << 8) | p[3]);
    };
    uint32_t ifd = u32(tiff + 4);
    const uint8_t* base = tiff;
    const uint8_t* p = base + ifd;
    const uint8_t* end = data.data() + data.size();
    if (p + 2 > end) return 1;
    uint16_t count = u16(p);
    p += 2;
    for (uint16_t i = 0; i < count; i++) {
        if (p + 12 > end) return 1;
        uint16_t tag = u16(p);
        uint16_t type = u16(p + 2);
        if (tag == 0x0112 && type == 3) return u16(p + 8);
        p += 12;
    }
    return 1;
}

QImage applyOrientation(const QImage& image, int orientation) {
    QImage out = image;
    switch (orientation) {
        case 2: out = out.flipped(Qt::Horizontal); break;
        case 3: out = out.transformed(QTransform().rotate(180)); break;
        case 4: out = out.flipped(Qt::Vertical); break;
        case 5: out = out.transformed(QTransform().rotate(90)).flipped(Qt::Horizontal); break;
        case 6: out = out.transformed(QTransform().rotate(90)); break;
        case 7: out = out.transformed(QTransform().rotate(-90)).flipped(Qt::Horizontal); break;
        case 8: out = out.transformed(QTransform().rotate(-90)); break;
        default: break;
    }
    return out;
}

} // namespace

bool heifSupported() { return true; }

bool isHeifPath(const QString& path) {
    return path.endsWith(".heic", Qt::CaseInsensitive) || path.endsWith(".heif", Qt::CaseInsensitive)
        || path.endsWith(".hif", Qt::CaseInsensitive) || path.endsWith(".avif", Qt::CaseInsensitive);
}

QImage readHeifImage(const QString& path, QString* error) {
    auto fail = [error](const QString& message) {
        if (error) *error = message;
        return QImage();
    };
    heif_context* context = heif_context_alloc();
    if (!context) return fail(QObject::tr("Out of memory reading the HEIC file."));
    struct Guard { heif_context* c; ~Guard() { heif_context_free(c); } } guard{context};

    const QByteArray encoded = QFile::encodeName(path);
    heif_error read = heif_context_read_from_file(context, encoded.constData(), nullptr);
    if (read.code != heif_error_Ok) return fail(QString::fromUtf8(read.message));

    heif_image_handle* handle = nullptr;
    heif_error primary = heif_context_get_primary_image_handle(context, &handle);
    if (primary.code != heif_error_Ok) return fail(QString::fromUtf8(primary.message));
    struct HandleGuard { heif_image_handle* h; ~HandleGuard() { heif_image_handle_release(h); } } handleGuard{handle};

    heif_image* decoded = nullptr;
    // Ask for RGBA straight away; libheif converts from the file's own chroma.
    heif_error decode = heif_decode_image(handle, &decoded, heif_colorspace_RGB, heif_chroma_interleaved_RGBA, nullptr);
    if (decode.code != heif_error_Ok) return fail(QString::fromUtf8(decode.message));
    struct ImageGuard { heif_image* i; ~ImageGuard() { heif_image_release(i); } } imageGuard{decoded};

    int width = heif_image_get_width(decoded, heif_channel_interleaved);
    int height = heif_image_get_height(decoded, heif_channel_interleaved);
    int stride = 0;
    const uint8_t* plane = heif_image_get_plane_readonly(decoded, heif_channel_interleaved, &stride);
    if (!plane || width <= 0 || height <= 0) return fail(QObject::tr("The HEIC file had no image data."));

    // libheif gives straight (unpremultiplied) RGBA; the rest of the app wants premultiplied.
    QImage image(width, height, QImage::Format_RGBA8888);
    for (int y = 0; y < height; y++) std::memcpy(image.scanLine(y), plane + ptrdiff_t(y) * stride, size_t(width) * 4);

    // EXIF lives in a metadata block of type "Exif"; find it, then read the orientation tag.
    int orientation = 1;
    heif_item_id ids[8];
    int count = heif_image_handle_get_list_of_metadata_block_IDs(handle, "Exif", ids, 8);
    if (count > 0) {
        heif_item_id id = ids[0];
        size_t size = heif_image_handle_get_metadata_size(handle, id);
        if (size > 0) {
            std::vector<uint8_t> metadata(size);
            if (heif_image_handle_get_metadata(handle, id, metadata.data()).code == heif_error_Ok)
                orientation = exifOrientation(metadata);
        }
    }

    // libheif reports the file's own colour profile; the app works in sRGB RGBA.
    if (image.colorSpace().isValid() && image.colorSpace() != QColorSpace(QColorSpace::SRgb))
        image.convertToColorSpace(QColorSpace(QColorSpace::SRgb));
    return applyOrientation(image, orientation).convertToFormat(QImage::Format_RGBA8888_Premultiplied);
}

#else

bool heifSupported() { return false; }
bool isHeifPath(const QString&) { return false; }

QImage readHeifImage(const QString&, QString* error) {
    if (error) *error = QObject::tr("This build has no HEIC support (libheif was not found).");
    return QImage();
}

#endif

} // namespace app
