// Optional HEIC/HEIF decoding through libheif, so the import formats match the
// Mac app's (JPEG, PNG, HEIC, TIFF) on platforms whose Qt has no HEIC plugin.
#pragma once
#include <QImage>
#include <QString>

namespace app {

/// True when this build can decode HEIC/HEIF files (libheif was found).
bool heifSupported();

/// Decodes the primary image of a HEIC/HEIF file. Returns a null QImage and sets
/// *error on failure, or when the build has no libheif.
QImage readHeifImage(const QString& path, QString* error = nullptr);

/// True when `path` looks like a HEIC/HEIF file and this build can decode it.
bool isHeifPath(const QString& path);

} // namespace app
