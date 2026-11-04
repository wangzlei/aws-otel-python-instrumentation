/*
 * Copyright Amazon.com, Inc. or its affiliates.
 *
 * Licensed under the Apache License, Version 2.0 (the "License").
 * You may not use this file except in compliance with the License.
 * A copy of the License is located at
 *
 *  http://aws.amazon.com/apache2.0
 *
 * or in the "license" file accompanying this file. This file is distributed
 * on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
 * express or implied. See the License for the specific language governing
 * permissions and limitations under the License.
 */
use std::collections::VecDeque;
use std::env;
use std::fs;
use std::io;
use std::os::unix;
use std::path::Path;
use std::path::PathBuf;
use std::process;

/// A type of copy operation
#[derive(Debug, PartialEq)]
enum CopyType {
    /// equivalent to cp <source> <dest>
    SingleFile,
    /// equivalent to cp -a <source> <dest>
    Archive,
    /// equivalent to cp -r <source> <dest>
    Recursive,
}

/// Encapsulate a copy operation
struct CopyOperation {
    /// The source path
    source: PathBuf,
    /// The destination path
    destination: PathBuf,
    /// The type of copy being performed
    copy_type: CopyType,
}

/// Parse command line arguments and transform into `CopyOperation`
fn parse_args(args: Vec<&str>) -> io::Result<CopyOperation> {
    if !(args.len() == 3 || (args.len() == 4 && (args[1] == "-a" || args[1] == "-r"))) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Invalid parameters. Expected cp [-a | -r] <source> <destination>",
        ));
    }

    if args.len() == 4 {
        return Ok(CopyOperation {
            source: PathBuf::from(args[2]),
            destination: PathBuf::from(args[3]),
            copy_type: match args[1] {
                "-a" => CopyType::Archive,
                "-r" => CopyType::Recursive,
                _ => panic!("Invalid option. Expected -a or -r"),
            },
        });
    }

    Ok(CopyOperation {
        source: PathBuf::from(args[1]),
        destination: PathBuf::from(args[2]),
        copy_type: CopyType::SingleFile,
    })
}

/// The OpenTelemetry injector is shipped once per architecture as `libotelinject_<arch>.so`.
/// While copying, only the file built for this machine is installed, as `libotelinject.so`;
/// the others are skipped. A wrong-architecture file in LD_PRELOAD makes musl refuse to start
/// the process, while a missing file is ignored, so never install a non-matching one.
/// This binary is statically linked for one architecture, so the architecture it was built
/// for is the one it runs on.
const INJECTOR_PREFIX: &str = "libotelinject_";
const INJECTOR_SUFFIX: &str = ".so";
const INJECTOR_NAME: &str = "libotelinject.so";

fn machine_arch_name() -> &'static str {
    match env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    }
}

/// Where a file named `name` goes in `dest_dir`: `None` to skip it.
fn injector_destination(dest_dir: &Path, name: &std::ffi::OsStr, arch: &str) -> Option<PathBuf> {
    if let Some(n) = name.to_str() {
        if let Some(a) = n
            .strip_prefix(INJECTOR_PREFIX)
            .and_then(|rest| rest.strip_suffix(INJECTOR_SUFFIX))
        {
            if a.is_empty() {
                return Some(dest_dir.join(name));
            }
            return if a == arch {
                Some(dest_dir.join(INJECTOR_NAME))
            } else {
                None
            };
        }
    }
    Some(dest_dir.join(name))
}

/// Execute the copy operation
fn do_copy(operation: CopyOperation) -> io::Result<()> {
    match operation.copy_type {
        CopyType::Archive => copy_archive(&operation.source, &operation.destination)?,
        CopyType::SingleFile => fs::copy(&operation.source, &operation.destination).map(|_| ())?,
        CopyType::Recursive => copy_recursive(&operation.source, &operation.destination)?,
    };
    Ok(())
}

/// Recreate the symbolic link `source` at `dest` with the same (possibly relative or dangling) target.
/// An existing file or link at `dest` is replaced, so copying into a volume that was filled before (an init
/// container that runs again) still succeeds; an existing real directory at `dest` is left as it is.
fn copy_symlink(source: &Path, dest: &Path) -> io::Result<()> {
    let target = source.read_link()?;
    if let Ok(existing) = fs::symlink_metadata(dest) {
        if existing.is_dir() {
            return Ok(());
        }
        fs::remove_file(dest)?;
    }
    unix::fs::symlink(target, dest)
}

fn copy_recursive(source: &Path, dest: &Path) -> io::Result<()> {
    let mut stack = VecDeque::new();
    stack.push_back((source.to_path_buf(), dest.to_path_buf()));
    while let Some((current_source, current_dest)) = stack.pop_back() {
        if current_source.is_symlink() {
            // Copy symbolic links as symbolic links, like `cp -r`: never follow them, so a link to a
            // directory (even a self-referencing one such as `glibc -> .`) is neither duplicated nor looped into.
            copy_symlink(&current_source, &current_dest)?;
        } else if current_source.is_dir() {
            if !current_dest.exists() {
                fs::create_dir(&current_dest)?;
            }
            for entry in fs::read_dir(current_source)? {
                let next_source = entry?.path();
                let name = next_source.file_name().ok_or(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "Invalid source file",
                ))?;
                if let Some(next_dest) =
                    injector_destination(&current_dest, name, machine_arch_name())
                {
                    stack.push_back((next_source, next_dest));
                }
            }
        } else if current_source.is_file() {
            fs::copy(current_source, current_dest)?;
        }
    }
    Ok(())
}

// Execute the recursive type of copy operation
fn copy_archive(source: &Path, dest: &Path) -> io::Result<()> {
    let mut stack = VecDeque::new();
    stack.push_back((source.to_path_buf(), dest.to_path_buf()));
    while let Some((current_source, current_dest)) = stack.pop_back() {
        if current_source.is_symlink() {
            copy_symlink(&current_source, &current_dest)?;
        } else if current_source.is_dir() {
            if !current_dest.exists() {
                fs::create_dir(&current_dest)?;
            }
            for entry in fs::read_dir(current_source)? {
                let next_source = entry?.path();
                let name = next_source.file_name().ok_or(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "Invalid source file",
                ))?;
                if let Some(next_dest) =
                    injector_destination(&current_dest, name, machine_arch_name())
                {
                    stack.push_back((next_source, next_dest));
                }
            }
        } else if current_source.is_file() {
            fs::copy(current_source, current_dest)?;
        }
    }
    Ok(())
}

fn main() {
    let original_args: Vec<String> = env::args().collect();
    let args = original_args.iter().map(|x| x.as_str()).collect();

    let operation = parse_args(args).unwrap_or_else(|err| {
        eprintln!("Error parsing arguments: {err}");
        process::exit(1);
    });

    do_copy(operation).unwrap_or_else(|err| {
        eprintln!("Error copying files: {err}");
        process::exit(2);
    });
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        io::Write,
        os::unix,
        path::{Path, PathBuf},
    };

    use crate::{do_copy, parse_args, CopyOperation, CopyType};
    use uuid;

    #[test]
    fn test_parser_archive() {
        // prepare
        let input = vec!["cp", "-a", "foo.txt", "dest.txt"];

        // act
        let result = parse_args(input).unwrap();

        // assert
        assert_eq!(result.source, PathBuf::from("foo.txt"));
        assert_eq!(result.destination, PathBuf::from("dest.txt"));
        assert_eq!(result.copy_type, CopyType::Archive)
    }

    #[test]
    fn test_parser_single() {
        // prepare
        let input: Vec<&str> = vec!["cp", "foo.txt", "dest.txt"];

        // act
        let result = parse_args(input).unwrap();

        // assert
        assert_eq!(result.source, PathBuf::from("foo.txt"));
        assert_eq!(result.destination, PathBuf::from("dest.txt"));
        assert_eq!(result.copy_type, CopyType::SingleFile)
    }

    #[test]
    fn parser_failure() {
        // prepare
        let inputs = vec![
            vec!["cp", "-r", "foo.txt", "bar.txt", "foo1.txt"],
            vec!["cp", "-a", "param1", "param2", "param3"],
            vec!["cp", "param1", "param2", "param3"],
        ];

        for input in inputs.into_iter() {
            // act
            let result = parse_args(input.clone());

            // assert
            assert!(result.is_err(), "input should fail {:?}", input);
        }
    }

    #[test]
    fn parser_correct() {
        // prepare
        let inputs = vec![
            vec!["cp", "-r", "foo.txt", "bar.txt"],
            vec!["cp", "-a", "param1", "param2"],
            vec!["cp", "param1", "param2"],
        ];

        for input in inputs.into_iter() {
            // act
            let result = parse_args(input.clone());

            // assert
            assert!(result.is_ok(), "input should fail {:?}", input);
        }
    }

    #[test]
    fn test_copy_single() {
        // prepare
        let tempdir = tempfile::tempdir().unwrap();
        let test_base = tempdir.path().to_path_buf();

        create_file(&test_base, "foo.txt");

        let source = test_base.join("foo.txt");
        let dest = test_base.join("bar.txt");
        let single_copy = CopyOperation {
            copy_type: CopyType::SingleFile,
            source: source.clone(),
            destination: dest.clone(),
        };

        // act
        do_copy(single_copy).unwrap();

        // assert
        assert_same_file(&source, &dest)
    }

    #[test]
    fn single_cannot_copy_directory() {
        // prepare
        let tempdir = tempfile::tempdir().unwrap();
        let test_base = tempdir.path().to_path_buf();

        create_dir(&test_base, "somedir");

        // act
        let single_copy = CopyOperation {
            copy_type: CopyType::SingleFile,
            source: test_base.join("somedir"),
            destination: test_base.join("somewhereelse"),
        };
        let result = do_copy(single_copy);

        // assert
        assert!(result.is_err());
    }

    #[test]
    fn test_copy_recursive() {
        // prepare
        let tempdir = tempfile::tempdir().unwrap();
        let test_base = tempdir.path().to_path_buf();
        ["foo", "foo/foo0", "foo/foo1", "foo/bar"]
            .iter()
            .for_each(|x| create_dir(&test_base, x));
        let files = [
            "foo/file1.txt",
            "foo/file2.txt",
            "foo/foo1/file3.txt",
            "foo/bar/file4.txt",
        ];
        files.iter().for_each(|x| create_file(&test_base, x));
        [("foo/symlink1.txt", "./file1.txt")]
            .iter()
            .for_each(|(x, y)| create_symlink(&test_base, x, y));

        // act
        let recursive_copy = CopyOperation {
            copy_type: CopyType::Recursive,
            source: test_base.join("foo"),
            destination: test_base.join("bar"),
        };
        do_copy(recursive_copy).unwrap();

        // assert
        files.iter().for_each(|x| {
            assert_same_file(
                &test_base.join(x),
                &test_base.join(x.replace("foo/", "bar/")),
            )
        });
        assert_same_file(
            &test_base.join("foo/symlink1.txt"),
            &test_base.join("bar/symlink1.txt"),
        );
        // recursive copy keeps symlinks as symlinks, like `cp -r`
        assert_same_link(
            &test_base.join("foo/symlink1.txt"),
            &test_base.join("bar/symlink1.txt"),
        )
    }

    #[test]
    fn test_copy_archive() {
        // prepare
        let tempdir = tempfile::tempdir().unwrap();
        let test_base = tempdir.path().to_path_buf();
        ["foo", "foo/foo0", "foo/foo1", "foo/bar"]
            .iter()
            .for_each(|x| create_dir(&test_base, x));
        let files = [
            "foo/file1.txt",
            "foo/file2.txt",
            "foo/foo1/file3.txt",
            "foo/bar/file4.txt",
        ];
        files.iter().for_each(|x| create_file(&test_base, x));
        [("foo/symlink1.txt", "./file1.txt")]
            .iter()
            .for_each(|(x, y)| create_symlink(&test_base, x, y));

        // act
        let recursive_copy = CopyOperation {
            copy_type: CopyType::Archive,
            source: test_base.join("foo"),
            destination: test_base.join("bar"),
        };
        do_copy(recursive_copy).unwrap();

        // assert
        files.iter().for_each(|x| {
            assert_same_file(
                &test_base.join(x),
                &test_base.join(x.replace("foo/", "bar/")),
            )
        });
        assert_same_file(
            &test_base.join("foo/symlink1.txt"),
            &test_base.join("bar/symlink1.txt"),
        );

        assert_same_link(
            &test_base.join("foo/symlink1.txt"),
            &test_base.join("bar/symlink1.txt"),
        )
    }

    #[test]
    fn test_copy_archive_destination_exists() {
        // prepare
        let tempdir = tempfile::tempdir().unwrap();
        let test_base = tempdir.path().to_path_buf();
        ["foo", "foo/foo0", "foo/foo1", "foo/bar"]
            .iter()
            .for_each(|x| create_dir(&test_base, x));
        let files = [
            "foo/file1.txt",
            "foo/file2.txt",
            "foo/foo1/file3.txt",
            "foo/bar/file4.txt",
        ];
        files.iter().for_each(|x| create_file(&test_base, x));
        [("foo/symlink1.txt", "./file1.txt")]
            .iter()
            .for_each(|(x, y)| create_symlink(&test_base, x, y));
        create_dir(&test_base, "bar");

        // act
        let recursive_copy = CopyOperation {
            copy_type: CopyType::Archive,
            source: test_base.join("foo"),
            destination: test_base.join("bar"),
        };
        do_copy(recursive_copy).unwrap();

        // assert
        files.iter().for_each(|x| {
            assert_same_file(
                &test_base.join(x),
                &test_base.join(x.replace("foo/", "bar/")),
            )
        });

        assert_same_link(
            &test_base.join("foo/symlink1.txt"),
            &test_base.join("bar/symlink1.txt"),
        )
    }

    // Utility functions used in the tests
    fn create_dir(base: &Path, dir: &str) {
        fs::create_dir_all(base.to_path_buf().join(dir)).unwrap();
    }

    fn create_file(base: &Path, file: &str) {
        let mut file = fs::File::create(base.to_path_buf().join(file)).unwrap();
        file.write_fmt(format_args!("{}", uuid::Uuid::new_v4().to_string()))
            .unwrap();
    }

    fn create_symlink(base: &Path, file: &str, target: &str) {
        unix::fs::symlink(Path::new(target), &base.to_path_buf().join(file)).unwrap();
    }

    fn assert_same_file(source: &Path, dest: &Path) {
        assert!(source.exists());
        assert!(dest.exists());
        assert!(source.is_file());
        assert!(dest.is_file());

        assert_eq!(
            fs::read_to_string(source).unwrap(),
            fs::read_to_string(dest).unwrap()
        );
    }

    fn assert_same_link(source: &Path, dest: &Path) {
        assert!(source.exists());
        assert!(dest.exists());
        assert!(source.is_symlink());
        assert!(dest.is_symlink());

        assert_eq!(fs::read_link(source).unwrap(), fs::read_link(dest).unwrap());
    }

    #[test]
    fn injector_destination_picks_matching_arch() {
        let d = Path::new("/dst");
        let f = |n: &str, a: &str| crate::injector_destination(d, std::ffi::OsStr::new(n), a);
        assert_eq!(
            f("libotelinject_amd64.so", "amd64"),
            Some(PathBuf::from("/dst/libotelinject.so"))
        );
        assert_eq!(f("libotelinject_arm64.so", "amd64"), None);
        assert_eq!(
            f("libotelinject_arm64.so", "arm64"),
            Some(PathBuf::from("/dst/libotelinject.so"))
        );
        assert_eq!(f("libotelinject_amd64.so", "arm64"), None);
        assert_eq!(
            f("libotelinject.so", "amd64"),
            Some(PathBuf::from("/dst/libotelinject.so"))
        );
        assert_eq!(
            f("libotelinject_.so", "amd64"),
            Some(PathBuf::from("/dst/libotelinject_.so"))
        );
        assert_eq!(f("other.so", "amd64"), Some(PathBuf::from("/dst/other.so")));
        assert_eq!(
            f("libotelinject_amd64.so.bak", "amd64"),
            Some(PathBuf::from("/dst/libotelinject_amd64.so.bak"))
        );
    }

    fn injector_tree_test(copy_type: CopyType) {
        let src = tempfile::tempdir().unwrap();
        let dst = tempfile::tempdir().unwrap();
        let inj = src.path().join("injector");
        fs::create_dir(&inj).unwrap();
        fs::write(inj.join("libotelinject_amd64.so"), b"amd64").unwrap();
        fs::write(inj.join("libotelinject_arm64.so"), b"arm64").unwrap();
        fs::write(inj.join("check-arch.sh"), b"sh").unwrap();
        fs::write(src.path().join("sdk.py"), b"py").unwrap();
        let out = dst.path().join("out");
        do_copy(CopyOperation {
            source: src.path().to_path_buf(),
            destination: out.clone(),
            copy_type,
        })
        .unwrap();
        let mine = crate::machine_arch_name();
        assert_eq!(
            fs::read(out.join("injector/libotelinject.so")).unwrap(),
            mine.as_bytes()
        );
        assert!(!out.join("injector/libotelinject_amd64.so").exists());
        assert!(!out.join("injector/libotelinject_arm64.so").exists());
        assert!(out.join("injector/check-arch.sh").exists());
        assert!(out.join("sdk.py").exists());
    }

    #[test]
    fn recursive_copy_installs_only_matching_injector() {
        injector_tree_test(CopyType::Recursive);
    }

    #[test]
    fn archive_copy_installs_only_matching_injector() {
        injector_tree_test(CopyType::Archive);
    }

    fn symlink_tree_test(copy_type: CopyType) {
        let src = tempfile::tempdir().unwrap();
        let dst = tempfile::tempdir().unwrap();
        fs::create_dir(src.path().join("linux-x64")).unwrap();
        fs::write(src.path().join("linux-x64/native.so"), b"so").unwrap();
        fs::write(src.path().join("sdk.py"), b"py").unwrap();
        unix::fs::symlink(".", src.path().join("glibc")).unwrap();
        unix::fs::symlink(".", src.path().join("musl")).unwrap();
        unix::fs::symlink("linux-x64", src.path().join("linux")).unwrap();
        unix::fs::symlink("does-not-exist", src.path().join("dangling")).unwrap();
        let out = dst.path().join("out");
        let copy = |t: CopyType| {
            do_copy(CopyOperation {
                source: src.path().to_path_buf(),
                destination: out.clone(),
                copy_type: t,
            })
        };
        copy(copy_type).unwrap();
        for (name, target) in [
            ("glibc", "."),
            ("musl", "."),
            ("linux", "linux-x64"),
            ("dangling", "does-not-exist"),
        ] {
            let p = out.join(name);
            assert!(p.is_symlink(), "{name} must stay a symlink");
            assert_eq!(fs::read_link(&p).unwrap(), PathBuf::from(target));
        }
        // the links resolve inside the copy, and nothing was duplicated through them
        assert_eq!(fs::read(out.join("glibc/sdk.py")).unwrap(), b"py");
        assert_eq!(fs::read(out.join("musl/linux/native.so")).unwrap(), b"so");
        let entries = fs::read_dir(&out).unwrap().count();
        assert_eq!(entries, 6);
        // copying again into the filled destination (an init container that runs again) succeeds
        copy(CopyType::Recursive).unwrap();
        assert_eq!(
            fs::read_link(out.join("glibc")).unwrap(),
            PathBuf::from(".")
        );
    }

    #[test]
    fn recursive_copy_keeps_symlinks_without_following_them() {
        symlink_tree_test(CopyType::Recursive);
    }

    #[test]
    fn archive_copy_keeps_symlinks_without_following_them() {
        symlink_tree_test(CopyType::Archive);
    }
}
